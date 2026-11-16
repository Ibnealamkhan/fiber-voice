import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from calls import Calls
from test_calls import FakeMedia, eventually
from test_conference import ConferenceRouter
from test_media import SDP


class HoldableMedia(FakeMedia):
    def __init__(self, address):
        super().__init__(address)
        self.video = None
        self.held = False


class GlareRouter(ConferenceRouter):
    """Refuses the first N re-INVITEs with 487, like the network when it updates the call at the same time."""

    def __init__(self, refusals=1):
        super().__init__('answer')
        self.refusals = refusals

    async def _send(self, message):
        if message.startswith('INVITE') and self.refusals:
            from registration import first, parse_headers
            start, headers = parse_headers(message.split('\r\n\r\n')[0].encode())
            if 'tag=' in first(headers, 'to'):
                self.refusals -= 1
                self.sent.append(message)
                self.answer(headers, 487, 'INVITE')
                return
        await super()._send(message)


class Sender:
    def __init__(self):
        self.track = type('Track', (), {'kind': 'audio'})()
        self.replaced = []

    def replaceTrack(self, track):
        self.replaced.append(track)


class Peer:
    """Stands in for the browser's WebRTC connection."""

    def __init__(self):
        self.sender = Sender()
        self.closed = False

    def getSenders(self):
        return [self.sender]

    async def close(self):
        self.closed = True


def invite_headers(number, call_id):
    return {'via': [f'SIP/2.0/TLS router.example.test;branch=z9hG4bK{call_id}'],
            'from': [f'<sip:{number}@example.test>;tag=t-{call_id}'], 'to': ['<sip:alice@example.test>'],
            'call-id': [call_id], 'cseq': ['1 INVITE'], 'contact': [f'<sip:{number}@router.example.test>']}


def in_dialog(call, headers, method, cseq):
    return {**headers, 'to': [f'<sip:alice@example.test>;tag={call.local_tag}'], 'cseq': [f'{cseq} {method}'],
            'via': [f'SIP/2.0/TLS router.example.test;branch=z9hG4bK{method.lower()}{cseq}']}


class CallWaitingTests(unittest.IsolatedAsyncioTestCase):
    async def connected(self):
        reg = ConferenceRouter('answer')
        calls = Calls(reg, HoldableMedia)
        first_call = await calls.prepare_outgoing('9876543210')
        first_call.invite_started = True
        first_call.peer = Peer()
        first_call.spawn(calls.invite(first_call))
        await eventually(lambda: first_call.state == 'active')
        return reg, calls, first_call

    async def ring_second(self, reg, calls, call_id='second'):
        headers = invite_headers('09123456789', call_id)
        await calls.incoming('INVITE sip:alice@example.test SIP/2.0', headers, SDP.encode())
        await eventually(lambda: any(s.startswith('SIP/2.0 180') and call_id in s for s in reg.sent))
        return calls.waiting, headers

    async def test_hold_and_answer_then_swap_and_end(self):
        reg, calls, first_call = await self.connected()
        try:
            waiting, headers = await self.ring_second(reg, calls)
            peer = first_call.peer
            await calls.answer_waiting(waiting)
            # The first call was put on hold with a re-INVITE (a=sendonly) and silenced.
            hold = next(s for s in reg.sent if s.startswith('INVITE') and f'Call-ID: {first_call.sip_id}' in s and 'a=sendonly' in s)
            self.assertIn('tag=remote-tag', hold)
            self.assertTrue(first_call.on_hold and first_call.media.held)
            # The browser connection now carries the new call, which was answered.
            self.assertIs(waiting.peer, peer)
            self.assertIsNone(first_call.peer)
            self.assertEqual(peer.sender.replaced[-1], waiting.media.track)
            self.assertTrue(any(s.startswith('SIP/2.0 200') and 'Call-ID: second' in s for s in reg.sent))
            self.assertIs(calls.current, waiting)
            self.assertIs(calls.held, first_call)
            await calls.incoming('ACK sip:alice@example.test SIP/2.0', in_dialog(waiting, headers, 'ACK', 1), b'')
            self.assertEqual(waiting.state, 'active')
            status = calls.status()
            self.assertEqual((status['call']['id'], status['held']['id'], status['waiting']), (waiting.id, first_call.id, None))
            # A third caller is busy while a call is on hold.
            await calls.incoming('INVITE sip:alice@example.test SIP/2.0', invite_headers('09000000003', 'third'), SDP.encode())
            self.assertTrue(any(s.startswith('SIP/2.0 486') and 'Call-ID: third' in s for s in reg.sent))

            # Swap: the second call goes on hold, the first resumes and gets the browser audio back.
            await calls.swap()
            self.assertIs(calls.current, first_call)
            self.assertIs(calls.held, waiting)
            self.assertIs(first_call.peer, peer)
            self.assertFalse(first_call.on_hold or first_call.media.held)
            self.assertTrue(waiting.on_hold and waiting.media.held)
            self.assertTrue(any(s.startswith('INVITE') and f'Call-ID: {first_call.sip_id}' in s and 'a=sendrecv' in s for s in reg.sent))
            self.assertTrue(any(s.startswith('INVITE') and 'Call-ID: second' in s and 'a=sendonly' in s for s in reg.sent))

            # Ending the connected call keeps the held one connected (still on hold) with the browser audio.
            await calls.hangup(first_call)
            self.assertIs(calls.current, waiting)
            self.assertIsNone(calls.held)
            self.assertIs(waiting.peer, peer)
            self.assertFalse(peer.closed)
            self.assertTrue(waiting.on_hold)
            await calls.resume(waiting, False)
            self.assertFalse(waiting.on_hold or waiting.media.held)
            await calls.hangup(waiting)
            self.assertTrue(peer.closed)
        finally:
            await calls.close()

    async def test_resume_retries_after_a_collision(self):
        reg = GlareRouter(refusals=0)
        calls = Calls(reg, HoldableMedia)
        call = await calls.prepare_outgoing('9876543210')
        call.invite_started = True
        call.spawn(calls.invite(call))
        await eventually(lambda: call.state == 'active')
        try:
            await calls.resume(call, True)
            reg.refusals = 1  # the network updates the call while we resume
            import calls as calls_module
            from unittest.mock import patch
            async def no_wait(_):
                return None
            with patch.object(calls_module.asyncio, 'sleep', no_wait):
                await calls.resume(call, False)
            reinvites = [s for s in reg.sent if s.startswith('INVITE') and 'tag=remote-tag' in s]
            self.assertEqual([('sendonly' if 'a=sendonly' in s else 'sendrecv') for s in reinvites], ['sendonly', 'sendrecv', 'sendrecv'])
            refused_seq = reinvites[1].split('CSeq: ', 1)[1].split()[0]
            self.assertTrue(any(s.startswith('ACK') and f'CSeq: {refused_seq} ACK' in s for s in reg.sent))  # the 487 was acknowledged
            self.assertFalse(call.on_hold or call.media.held)
        finally:
            await calls.close()

    async def test_reinvite_without_sdp_gets_our_offer_and_ack_answer(self):
        reg, calls, call = await self.connected()
        try:
            def network(method, cseq, body=b''):
                headers = {'via': [f'SIP/2.0/TLS router.example.test;branch=z9hG4bKnet{cseq}'],
                           'from': ['<sip:remote@router.example.test>;tag=remote-tag'], 'to': [call.local_from],
                           'call-id': [call.sip_id], 'cseq': [f'{cseq} {method}']}
                return calls.incoming(f'{method} sip:alice@[::1]:25000 SIP/2.0', headers, body)
            await network('INVITE', 20)
            ok = reg.sent[-1]
            self.assertTrue(ok.startswith('SIP/2.0 200'))
            self.assertIn('Content-Type: application/sdp', ok)
            self.assertIn('a=sendrecv', ok)  # not on hold: offer two-way audio
            self.assertTrue(call.offer_pending)
            negotiated = []
            call.media.negotiate = lambda sdp, incoming=False: negotiated.append((sdp, incoming))
            await network('ACK', 20, SDP.encode())
            self.assertEqual(negotiated, [(SDP, True)])  # the network's answer, from the ACK
            self.assertFalse(call.offer_pending)
            await calls.resume(call, True)
            await network('INVITE', 21)
            self.assertIn('a=sendonly', reg.sent[-1])  # on hold: the offer keeps the hold
        finally:
            await calls.close()

    async def test_end_and_answer(self):
        reg, calls, first_call = await self.connected()
        try:
            waiting, headers = await self.ring_second(reg, calls)
            self.assertIs(calls.current, first_call)
            self.assertEqual(waiting.state, 'incoming')
            self.assertEqual(calls.status()['waiting']['id'], waiting.id)
            self.assertFalse(any(s.startswith('SIP/2.0 486') for s in reg.sent))
            # End & Answer: hang up the connected call, then the waiting call rings as the only call.
            await calls.hangup(first_call)
            self.assertEqual(first_call.state, 'ended')
            self.assertIs(calls.current, waiting)
            self.assertIsNone(calls.status()['waiting'])
            await calls.answer(waiting)
            self.assertTrue(any(s.startswith('SIP/2.0 200') and 'Call-ID: second' in s for s in reg.sent))
            await calls.incoming('ACK sip:alice@example.test SIP/2.0', in_dialog(waiting, headers, 'ACK', 1), b'')
            self.assertEqual(waiting.state, 'active')
        finally:
            await calls.close()

    async def test_declining_or_caller_hanging_up_leaves_the_call(self):
        reg, calls, first_call = await self.connected()
        try:
            waiting, _ = await self.ring_second(reg, calls)
            await calls.hangup(waiting)  # Decline
            self.assertTrue(any(s.startswith('SIP/2.0 486') and 'Call-ID: second' in s for s in reg.sent))
            self.assertIsNone(calls.waiting)
            self.assertIs(calls.current, first_call)
            self.assertEqual(first_call.state, 'active')
            self.assertFalse(first_call.on_hold)

            again, headers = await self.ring_second(reg, calls, 'third')
            await calls.incoming('CANCEL sip:alice@example.test SIP/2.0', {**headers, 'cseq': ['1 CANCEL']}, b'')
            self.assertEqual(again.state, 'cancelled')
            self.assertIsNone(calls.waiting)
            self.assertIs(calls.current, first_call)
        finally:
            await calls.close()

    async def test_third_call_and_calls_during_setup_are_busy(self):
        reg, calls, first_call = await self.connected()
        try:
            await self.ring_second(reg, calls)
            await calls.incoming('INVITE sip:alice@example.test SIP/2.0', invite_headers('09000000001', 'third'), SDP.encode())
            self.assertTrue(any(s.startswith('SIP/2.0 486') and 'Call-ID: third' in s for s in reg.sent))
            with self.assertRaises(ValueError):
                await calls.add_participant(first_call, '9111111111')
        finally:
            await calls.close()
        reg = ConferenceRouter('answer')
        calls = Calls(reg, HoldableMedia)
        try:
            dialing = await calls.prepare_outgoing('9876543210')  # not connected yet
            await calls.incoming('INVITE sip:alice@example.test SIP/2.0', invite_headers('09000000002', 'early'), SDP.encode())
            self.assertTrue(any(s.startswith('SIP/2.0 486') and 'Call-ID: early' in s for s in reg.sent))
            self.assertIs(calls.current, dialing)
        finally:
            await calls.close()

    async def test_hold_and_resume_the_only_call(self):
        reg, calls, first_call = await self.connected()
        try:
            await calls.resume(first_call, True)
            self.assertTrue(first_call.on_hold and first_call.media.held)
            await calls.resume(first_call, False)
            self.assertFalse(first_call.on_hold or first_call.media.held)
            directions = [d for s in reg.sent if s.startswith('INVITE') and 'tag=remote-tag' in s
                          for d in ('a=sendonly', 'a=sendrecv') if d in s]
            self.assertEqual(directions, ['a=sendonly', 'a=sendrecv'])
        finally:
            await calls.close()


if __name__ == '__main__':
    unittest.main()
