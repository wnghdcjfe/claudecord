GREETING_TRIGGER = "안녕"
GREETING_REPLY = "안녕하세요 주인님. 저는 주인님의 AI비서, 자비스입니다."


def direct_reply_for(text: str) -> str | None:
    if text.strip() == GREETING_TRIGGER:
        return GREETING_REPLY
    return None
