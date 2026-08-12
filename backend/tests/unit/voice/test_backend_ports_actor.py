"""The voice runtime's ActorContext must declare the VOICE channel (CEO #33).

CEO #33's disambiguation give-up wording is channel-specific: on TEXT the patient
can be told to "call the clinic", but on VOICE the caller is already connected, so
that instruction is a false one. The routing keys off ActorContext.channel, which
is authoritative and set by the transport layer. build_actor_context is that layer
for voice — if it ever stops setting channel=VOICE, a voice caller silently gets
the text give-up and the exact defect #33 fixed comes back through this door. This
test locks it.
"""

from fonely.models.enums import CallerRole, Channel
from fonely.voice.backend_ports import build_actor_context


def test_voice_actor_declares_voice_channel() -> None:
    actor = build_actor_context(business_id=1, phone="+919123456789", session_id="sess-1")
    assert actor.channel is Channel.VOICE, (
        "the voice runtime's ActorContext must set channel=VOICE, or a voice "
        "caller gets the text give-up ('call the clinic' — the number they are "
        "already on): the CEO #33 defect, resurrected at the transport boundary"
    )
    # Sanity: the rest of the trusted identity is intact.
    assert actor.business_id == 1
    assert actor.verified_role is CallerRole.CUSTOMER
    assert actor.session_id == "sess-1"
