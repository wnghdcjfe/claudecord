import os
from dataclasses import dataclass
from pathlib import Path

PROJECTS = {
    "@book": ("book", "길벗 출판사용 클로드코드 책 원고. 한국어로 답변, 변경 시 diff 표시."),
    "@md2short": ("md2short", "Remotion + ElevenLabs 비디오 파이프라인."),
    "@avisspick": ("avisspick", "어비스픽 투자 리포트 서비스. 보안 정보 출력 금지."),
}


@dataclass
class Command:
    prompt: str
    session_id: str | None = None
    workdir: str | None = None
    system_hint: str | None = None


def _project_dir(tag: str) -> str:
    dirname = PROJECTS[tag][0]
    project_root = Path(os.environ.get("PROJECT_ROOT", "~/projects")).expanduser()
    return str(project_root / dirname)


def project_definitions() -> dict[str, tuple[str, str]]:
    return {
        tag: (_project_dir(tag), hint)
        for tag, (_, hint) in PROJECTS.items()
    }


def parse(text: str) -> Command:
    text = text.strip()
    if text.startswith("@sess-"):
        head, _, rest = text.partition(" ")
        return Command(prompt=rest, session_id=head[1:])

    for tag, (workdir, hint) in project_definitions().items():
        if text.startswith(tag + " "):
            return Command(
                prompt=text[len(tag) + 1:],
                workdir=workdir,
                system_hint=hint,
            )

    return Command(prompt=text)
