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
from src.status import (
    format_working_status,
    run_spinning_loader,
    stop_spinning_loader,
)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"[bot] logged in as {client.user}")


@client.event
async def on_message(msg: discord.Message):
    if not is_authorized(msg):
        return
    if not msg.clean_content.strip():
        return

    direct_reply = direct_reply_for(msg.clean_content)
    if direct_reply:
        await msg.reply(direct_reply)
        return

    cmd = parse(msg.clean_content)
    try:
        job_dir = create_job(cmd.prompt, cmd.workdir, cmd.system_hint)
    except ValueError as exc:
        await msg.reply(f"실행 불가: {exc}")
        return

    ack = await msg.reply(format_working_status(job_dir.name))
    loader_task = asyncio.create_task(run_spinning_loader(ack, job_dir.name))

    try:
        meta = await run_job(job_dir, resume=cmd.session_id)
    except Exception as exc:
        await stop_spinning_loader(loader_task)
        await ack.edit(content=f"작업 실패 · {job_dir.name}")
        await msg.channel.send(f"내부 오류: {exc}")
        return

    await stop_spinning_loader(loader_task)
    failed = meta.get("type") == "error" or meta.get("is_error")
    status = "작업 실패" if failed else "작업 완료"
    await ack.edit(content=f"{status} · {job_dir.name}")
    if failed and meta.get("text"):
        await msg.channel.send(str(meta["text"])[:1900])
    await send_outputs(msg.channel, job_dir)


def main():
    client.run(os.environ["DISCORD_BOT_TOKEN"])


if __name__ == "__main__":
    main()
