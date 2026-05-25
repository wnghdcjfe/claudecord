import asyncio

import discord

WORKING_MESSAGE = "열심히 작업중입니다."
SPINNER_INTERVAL_SECONDS = 1.2
STATUS_FRAMES = (
    ("⚙️", "▰▱▱▱▱", "요청을 정리하는 중"),
    ("🛠️", "▰▰▱▱▱", "작업을 수행하는 중"),
    ("🔧", "▰▰▰▱▱", "결과를 다듬는 중"),
    ("🚧", "▰▰▰▰▱", "꼼꼼히 확인하는 중"),
    ("✨", "▰▰▰▰▰", "마무리하는 중"),
)


def format_working_status(job_name: str, frame_index: int = 0) -> str:
    icon, activity_bar, activity_label = STATUS_FRAMES[frame_index % len(STATUS_FRAMES)]
    return (
        f"{icon} **{WORKING_MESSAGE}**\n"
        f"{activity_bar} {activity_label}\n"
        f"`{job_name}`"
    )


async def run_spinning_loader(
    message: discord.Message,
    job_name: str,
    *,
    interval: float = SPINNER_INTERVAL_SECONDS,
) -> None:
    frame_index = 1
    while True:
        await asyncio.sleep(interval)
        try:
            await message.edit(content=format_working_status(job_name, frame_index))
        except discord.DiscordException:
            return
        frame_index = (frame_index + 1) % len(STATUS_FRAMES)


async def stop_spinning_loader(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
