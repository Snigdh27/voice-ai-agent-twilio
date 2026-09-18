from dotenv import load_dotenv
load_dotenv()

from tac import TAC, TACConfig
from tac.channels.voice import VoiceChannel
from tac.channels.voice.media_streams.gpt_live import (
    TWILIO_AUDIO_FORMAT_FOR_GPT_LIVE,
    GPTLiveProviderConfig,
)
from tac.server import TACFastAPIServer


tac = TAC(config=TACConfig.from_env())


SESSION_CONFIG = {
    "model": "gpt-live-1",

    "instructions": (
        "You are a friendly live voice assistant. "
        "Talk naturally with the caller. "
        "Keep responses short and conversational. "
        "If the caller interrupts, stop and listen. "
        "You can answer general questions directly."
    ),

    "audio": {
        "format": TWILIO_AUDIO_FORMAT_FOR_GPT_LIVE,

        "output": {
            "voice": "marin"
        }
    }
}


voice_channel = VoiceChannel(
    tac,
    config=GPTLiveProviderConfig(
        default_session_config=SESSION_CONFIG,

        welcome_instruction=(
            "Greet the caller briefly and ask how you can help."
        )
    )
)


server = TACFastAPIServer(
    tac=tac,
    voice_channel=voice_channel,
    app=None
)


if __name__ == "__main__":
    server.start()