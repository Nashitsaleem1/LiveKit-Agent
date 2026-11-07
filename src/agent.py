import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    FunctionToolsExecutedEvent,
    JobContext,
    RunContext,
    STTContextOptions,
    TurnHandlingOptions,
    cli,
    function_tool,
    inference,
    room_io,
)
from livekit.agents.beta.tools import EndCallTool
from livekit.agents.llm import Toolset
from livekit.plugins import ai_coustics

import live_data
from claude_skill import run_skill
from internal_api import api_get
from knowledge import get_kb

logger = logging.getLogger("agent")
_background: set[asyncio.Task] = set()  # keeps fire-and-forget tasks alive until done
REJOIN_GRACE = 20  # seconds to wait for a caller who dropped off before ending the call

load_dotenv(".env.local")

PROMPT_PATH = Path(__file__).parent / "prompt.md"

# Spoken while the Claude risk review runs, only during quiet pauses.
RISK_REVIEW_FILLERS = [
    "Still working on the risk review, one moment.",
    "The analysis is still running, almost there.",
    "Thanks for waiting, just finishing up.",
]


GOODBYE = "Thanks for checking in. Have a great day, goodbye!"


async def _say_goodbye(ev: Toolset.ToolCalledEvent) -> None:
    # Wait until the goodbye has fully played before the call is closed
    await ev.ctx.session.say(GOODBYE, allow_interruptions=False)


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            # A Large Language Model (LLM) is your agent's brain, processing user input and generating a response
            # See all available models at https://docs.livekit.io/agents/models/llm/
            llm=inference.LLM(model="google/gemma-4-31b-it"),
            # To use a realtime model instead of a voice pipeline, replace the LLM
            # with a realtime model and remove the STT/TTS from the AgentSession
            # (Note: This is for OpenAI GPT-Live, the recommended speech-to-speech
            # model. For other providers, see https://docs.livekit.io/agents/models/realtime/)
            # Replace the llm argument with the model's LiveKit Inference ID:
            #    llm="openai/gpt-live-1",
            # The system prompt lives in src/prompt.md so it can be edited without touching code.
            instructions=PROMPT_PATH.read_text(encoding="utf-8"),
            # Lets Ava hang up: she always speaks GOODBYE (and waits for it to finish playing),
            # then the room is closed for everyone. A fixed line is used because the LLM
            # sometimes skips the goodbye when it calls the tool.
            tools=EndCallTool(
                extra_description=(
                    "Use it when the user says they are done, says goodbye, or answers no "
                    "when asked if they need anything else. Don't say goodbye yourself; "
                    "the tool says it."
                ),
                on_tool_called=_say_goodbye,
                end_instructions=None,
                delete_room=True,
            ).tools,
        )

    # ---------------------------------------------------------------------------
    # Tools. The docstring of each tool is what the LLM reads to decide when to
    # use it, so keep it specific. All tools are read-only.
    # ---------------------------------------------------------------------------

    # Knowledge base (kb/*.md)
    @function_tool()
    async def search_knowledge(self, context: RunContext, query: str) -> str:
        """Search the company handbook: company facts, locations, office hours, goals,
        leadership and who to contact, services and key customers, policies (time off,
        expenses, travel, IT security, safety) and frequently asked questions.
        Use it for any question about how the company works. Not for live numbers.

        Args:
            query: A few keywords, for example "expense approval limits" or "Houston warehouse manager".
        """
        results = get_kb().search(query, top_k=3)
        if not results:
            return "No matching notes in the handbook."
        return "\n\n".join(s.render() for s in results)

    # Internal company API (live data, read-only)
    @function_tool()
    async def get_morning_brief(self, context: RunContext) -> dict:
        """Get today's morning brief: yesterday's revenue against target, deliveries and
        on-time rate, open and high-priority support tickets, delayed shipments right now,
        overnight incidents, the first meeting today, and live weather with travel risks at
        the Dallas, Houston and Phoenix warehouses. Use when the user asks for the brief, a
        summary of the day, or how things are going."""
        # Company data and live weather are fetched in parallel to keep the brief fast
        brief, weather = await asyncio.gather(
            api_get("/api/v1/brief/daily"), live_data.warehouse_weather_for_brief()
        )
        return {**brief, "warehouse_weather": weather}

    @function_tool()
    async def get_sales_summary(
        self, context: RunContext, period: Literal["yesterday", "week", "month"]
    ) -> dict:
        """Get revenue, target, change versus the previous period and top customers.

        Args:
            period: "yesterday", "week" (last 7 days) or "month" (last 30 days).
        """
        return await api_get("/api/v1/sales/summary", {"period": period})

    @function_tool()
    async def get_support_tickets(
        self, context: RunContext, priority: Literal["high", "all"] = "high"
    ) -> dict:
        """Get open customer support tickets with customer, subject, priority and age.

        Args:
            priority: "high" for only high-priority tickets (default), or "all".
        """
        data = await api_get("/api/v1/support/tickets", {"priority": priority})
        data["tickets"] = data["tickets"][:6]  # keep it short for speech
        return data

    @function_tool()
    async def get_delayed_shipments(self, context: RunContext) -> dict:
        """Get shipments that are delayed right now, with customer, route, delay and reason."""
        return await api_get("/api/v1/shipments/delayed")

    @function_tool()
    async def get_todays_meetings(self, context: RunContext) -> dict:
        """Get today's leadership calendar: meeting times, titles and attendees."""
        return await api_get("/api/v1/calendar/today")

    # Real-time public data (live weather via Open-Meteo, local time)
    @function_tool()
    async def get_weather(self, context: RunContext, city: str) -> dict:
        """Get live weather for any city: current conditions and temperature, today's
        high, low and rain chance, and anything that could slow down trucks.

        Args:
            city: City name, with state or country if ambiguous, for example "Austin" or "Paris, France".
        """
        return await live_data.get_weather(city)

    @function_tool()
    async def get_warehouse_weather(self, context: RunContext) -> dict:
        """Get live weather and travel risks (storms, ice, fog, heavy rain, strong wind) for all
        three warehouses at once: Dallas, Houston and Phoenix. Use when the user asks about
        weather at our sites or whether weather could delay deliveries today."""
        return await live_data.get_warehouse_weather()

    @function_tool()
    async def get_local_time(self, context: RunContext, city: str = "") -> dict:
        """Get the current local date and time. Use for any question about today's date, the
        time, or the time somewhere else.

        Args:
            city: City to get the time for. Empty for headquarters in Austin.
        """
        return await live_data.get_local_time(city or None)

    # Claude webhook: one predefined, read-only analysis skill. Slow (10 to 60 s), so it
    # runs as an async tool: Ava acknowledges right away and keeps the user posted.
    @function_tool(on_duplicate="reject")
    async def run_risk_review(self, context: RunContext, focus: str = "") -> str:
        """Ask the company's analysis assistant to run the weekly operations risk review.
        It analyzes shipments, support tickets and sales and returns the top risks and
        recommendations. It takes up to a minute. Use only when the user asks for a deeper
        analysis, a risk review, or what to worry about this week.

        Args:
            focus: Optional area to emphasize, for example "Houston warehouse" or "support backlog". Empty for everything.
        """
        await context.update(
            "Started the weekly operations risk review with the analysis assistant. "
            "It usually takes under a minute. Tell the user you're working on it."
        )
        async with context.with_filler(
            lambda step: RISK_REVIEW_FILLERS[step],
            delay=8,
            interval=12,
            max_steps=len(RISK_REVIEW_FILLERS),
        ):
            result = await run_skill(focus or None)
        return json.dumps(result)


server = AgentServer()


@server.rtc_session(agent_name="brief-agent")
async def brief_agent(ctx: JobContext):
    # Logging setup
    # Add any other context you want in all log entries here
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    # Set up a voice AI pipeline using AssemblyAI, Fish Audio, and the LiveKit turn detector
    session = AgentSession(
        # Speech-to-text (STT) is your agent's ears, turning the user's speech into text that the LLM can understand
        # See all available models at https://docs.livekit.io/agents/models/stt/
        stt=inference.STT(model="assemblyai/universal-3-6-pro", language="en"),
        # Keyterms bias the STT toward distinctive words it would otherwise misspell.
        # List your own names, brands, and jargon in `keyterms`. Detection additionally
        # extracts terms from the live conversation, such as a caller's name, and applies
        # them once the transcript corroborates the spelling.
        # See more at https://docs.livekit.io/agents/models/stt/keyterms/
        stt_context_options=STTContextOptions(
            keyterms=[
                "Ava",
                "Harbor Point",
                "Harbor Track",
                "Brightwell",
                "Kestrel",
                "Novaline",
                "Greenleaf",
            ],
            keyterm_detection={"enabled": True},
        ),
        # Text-to-speech (TTS) is your agent's voice, turning the LLM's text into speech that the user can hear
        # See all available models as well as voice selections at https://docs.livekit.io/agents/models/tts/
        # Voice: Inworld "Elizabeth" (professional, mature female) on the low-latency Flash model.
        # In a live call the reply is spoken sentence by sentence, so each sentence's start-up
        # time matters: the full inworld-tts-2 model left pauses of up to 1.4 s between
        # sentences, Flash keeps them near zero. Test any new voice for this before switching.
        # To change it, pick a model + voice from https://docs.livekit.io/agents/models/tts/
        tts=inference.TTS(
            model="inworld/inworld-tts-2-flash",
            voice="Elizabeth",
            language="en",
        ),
        turn_handling=TurnHandlingOptions(
            # The LiveKit turn detector determines when the user is done speaking and the agent should respond.
            # TurnDetector is an end-of-turn model that listens to the user's audio directly, combining
            # semantic understanding with acoustic cues (intonation, pitch, rhythm) for state-of-the-art accuracy.
            # AgentSession supplies the required VAD automatically.
            # See more at https://docs.livekit.io/agents/build/turns
            turn_detection=inference.TurnDetector(),
            # Adaptive interruptions use the turn detector to tell a real interruption from a
            # backchannel like "mhm" or "right", so the agent keeps talking through the latter.
            # This model is on by default on LiveKit Cloud and in dev mode. To use the model elsewhere,
            # uncomment the `interruption` line. Self-hosted usage past the monthly allowance is billed.
            # See https://docs.livekit.io/deploy/admin/quotas-and-limits/#adaptive-interruption-handling for more information.
            # interruption={"mode": "adaptive"},
            # allow the LLM to generate a response while waiting for the end of turn
            # See more at https://docs.livekit.io/agents/build/audio/#preemptive-generation
            preemptive_generation={"enabled": True},
        ),
        # Expressive mode injects the TTS provider's markup guide into the LLM prompt, so the model
        # emits inline delivery tags (emotion, pacing, non-verbal sounds) that the TTS renders and
        # the transcript never shows. Off here for a steady, professional delivery.
        # See https://docs.livekit.io/agents/models/tts/expressive/
        expressive=False,
    )

    # Start the session, which initializes the voice pipeline and warms up the models
    # Noise cancellation cleans up the caller's audio (useful in a car). It is CPU-heavy, and on
    # a slow dev machine it can stall the agent and make speech choppy, so it can be turned off
    # with NOISE_CANCELLATION=off in .env.local.
    room_options = room_io.RoomOptions(
        # On a phone in a car the connection often drops for a moment and the browser rejoins,
        # which looks like the user leaving. Don't end the call then: wait for them to come
        # back (see _on_participant_disconnected below).
        close_on_disconnect=False,
        # When Ava hangs up, end the call for the user right away. Without this the room is
        # only deleted at job shutdown, after the session report upload (up to 20 s later).
        delete_room_on_close=True,
    )
    if os.getenv("NOISE_CANCELLATION", "on").lower() != "off":
        room_options.audio_input = room_io.AudioInputOptions(
            noise_cancellation=ai_coustics.audio_enhancement(
                model=ai_coustics.EnhancerModel.QUAIL_VF_S
            ),
        )

    await session.start(
        agent=Assistant(),
        room=ctx.room,
        room_options=room_options,
    )

    # The call page sends "interrupt" when the user taps the circle to stop Ava talking.
    @ctx.room.local_participant.register_rpc_method("interrupt")
    async def _on_interrupt(data: rtc.RpcInvocationData) -> str:
        logger.info("interrupt requested by %s", data.caller_identity)
        # force: an explicit tap should stop her even during the start-of-call warm-up
        session.interrupt(force=True)
        return "ok"

    # Report every tool call in the log and to the call page (text topic "ava.tools"),
    # so you can see which tools Ava used. The demo recording shows them on screen.
    async def _report_tools(calls: list[dict]) -> None:
        # the call may already be over (end_call), so ignore send errors
        with contextlib.suppress(Exception):
            await ctx.room.local_participant.send_text(
                json.dumps(calls), topic="ava.tools"
            )

    # If the caller drops off, give them REJOIN_GRACE seconds to come back (a network blip
    # makes the browser rejoin automatically) before ending the call.
    rejoin_timer: asyncio.TimerHandle | None = None

    def _callers() -> list[rtc.RemoteParticipant]:
        return [
            p
            for p in ctx.room.remote_participants.values()
            if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        ]

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(_p: rtc.RemoteParticipant) -> None:
        nonlocal rejoin_timer
        if _callers() or rejoin_timer:
            return
        logger.info(
            "caller left; ending the call unless they rejoin in %ss", REJOIN_GRACE
        )
        rejoin_timer = asyncio.get_running_loop().call_later(
            REJOIN_GRACE, lambda: session.shutdown()
        )

    @ctx.room.on("participant_connected")
    def _on_participant_connected(_p: rtc.RemoteParticipant) -> None:
        nonlocal rejoin_timer
        if rejoin_timer and _callers():
            logger.info("caller rejoined")
            rejoin_timer.cancel()
            rejoin_timer = None

    @session.on("function_tools_executed")
    def _on_tools_executed(ev: FunctionToolsExecutedEvent) -> None:
        calls = [{"name": c.name, "args": c.arguments} for c in ev.function_calls]
        logger.info("tools executed: %s", ", ".join(c["name"] for c in calls))
        task = asyncio.create_task(_report_tools(calls))
        _background.add(task)
        task.add_done_callback(_background.discard)

    # # Add a virtual avatar to the session, if desired
    # # For other providers, see https://docs.livekit.io/agents/models/avatar/
    # avatar = anam.AvatarSession(
    #     persona_config=anam.PersonaConfig(
    #         name="...",
    #         avatarId="...",  # See https://docs.livekit.io/agents/models/avatar/plugins/anam
    #     ),
    # )
    # # Start the avatar and wait for it to join
    # await avatar.start(session, room=ctx.room)

    # Join the room and connect to the user
    await ctx.connect()

    # Speak first: greet the user before they say anything
    await session.generate_reply(
        instructions="Greet the user in one short sentence, introduce yourself as Ava from Harbor Point, and offer the morning brief."
    )


if __name__ == "__main__":
    cli.run_app(server)
