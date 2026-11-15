import asyncio
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from calls import Calls, network_identity
from test_media import SDP
from registration import first, parse_headers
from test_calls import FakeMedia, FakeRegistration, eventually


class ConferenceRouter(FakeRegistration):
    """FakeRegistration that also handles REFER and in-dialog re-INVITE (hold)."""

    def __init__(self, scenario='answer', refer_failures=0, busy=()):
        super().__init__(scenario)
        self.refer_failures = refer_failures
        self.busy = busy
        self.last_call_id = None

    def reply(self, method, seq, code, extra=(), body=''):
        # Like FakeRegistration.reply, but on the Call-ID of the request being answered.
        raw = (f'SIP/2.0 {code} Result\r\nCall-ID: {self.last_call_id}\r\nCSeq: {seq} {method}\r\nTo: <sip:bob@example.test>;tag=remote-tag\r\n'
               'Contact: <sip:remote@router.example.test;transport=tls>\r\n' + ''.join(e + '\r\n' for e in extra) +
               'Content-Length: ' + str(len(body.encode())) + '\r\n\r\n' + body)
        start, headers = parse_headers(raw.split('\r\n\r\n')[0].encode())
        asyncio.create_task(self.call_handler.response(start, headers, body.encode()))

    def answer(self, headers, code, method, body=''):
        raw = (f'SIP/2.0 {code} Result\r\nCall-ID: {first(headers, "call-id")}\r\nCSeq: {first(headers, "cseq")}\r\n'
               'To: <sip:bob@example.test>;tag=remote-tag\r\nContent-Length: ' + str(len(body)) + '\r\n\r\n' + body)
        start, parsed = parse_headers(raw.split('\r\n\r\n')[0].encode())
        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.call_handler.response(start, parsed, body.encode())))

    async def _send(self, message):
        start, headers = (parse_headers(message.split('\r\n\r\n')[0].encode()) if not message.startswith('SIP/') else (None, None))
        if start and start.startswith('REFER'):
            self.sent.append(message)
            if self.refer_failures:
                self.refer_failures -= 1
                self.answer(headers, 500, 'REFER')
            else:
                self.answer(headers, 202, 'REFER')
            return
        if start:
            self.last_call_id = first(headers, 'call-id')
        if start and start.startswith('INVITE') and any(n in start for n in self.busy) and 'tag=' not in first(headers, 'to'):
            self.sent.append(message)
            self.answer(headers, 486, 'INVITE')
            return
        if start and start.startswith('INVITE') and 'tag=' in first(headers, 'to'):
            self.sent.append(message)  # re-INVITE (hold)
            self.answer(headers, 200, 'INVITE', SDP.replace('a=sendrecv', 'a=recvonly'))
            return
        await super()._send(message)


def in_dialog(call, method, cseq, extra=None, tag='remote-tag'):
    headers = {'via': ['SIP/2.0/TLS router.example.test;branch=z9hG4bK' + method.lower() + str(cseq)],
               'from': [f'<sip:remote@router.example.test>;tag={tag}'], 'to': [call.local_from],
               'call-id': [call.sip_id], 'cseq': [f'{cseq} {method}']}
    headers.update(extra or {})
    return headers


class ConferenceTests(unittest.IsolatedAsyncioTestCase):
    async def connected_call(self, refer_failures=0, busy=()):
        reg = ConferenceRouter('answer', refer_failures, busy)
        calls = Calls(reg, FakeMedia)
        call = await calls.prepare_outgoing('9876543210')
        call.invite_started = True
        call.spawn(calls.invite(call))
        await eventually(lambda: call.state == 'active')
        return reg, calls, call

    async def test_call_becomes_conference_and_new_person_is_called_then_merged(self):
        reg, calls, call = await self.connected_call()
        try:
            conf = await calls.add_participant(call, '9123456789')
            self.assertTrue(conf.conference)
            self.assertIs(calls.current, conf)
            invite = next(s for s in reg.sent if s.startswith('INVITE sip:mmtel@conf-factory.example.test'))
            self.assertIn('P-Preferred-Service: urn:urn-7:3gpp-service.ims.icsi.mmtel', invite)
            await eventually(lambda: len([s for s in reg.sent if s.startswith('REFER')]) >= 2)
            await eventually(lambda: conf.adding is None)
            refers = [s for s in reg.sent if s.startswith('REFER')]
            self.assertTrue(all(r.startswith('REFER sip:remote@router.example.test') for r in refers))
            self.assertTrue(all(f'Call-ID: {conf.sip_id}' in r for r in refers))
            move, merge = refers
            escaped = call.sip_id.replace('@', '%40')
            self.assertIn(f'Refer-To: <sip:+919876543210@example.test;user=phone;method=INVITE?Replaces={escaped}%3Bto-tag%3Dremote-tag%3Bfrom-tag%3D{call.local_tag}>', move)
            # The new person is called by us first (phone style), then their call is merged with Replaces.
            side_invite = next(s for s in reg.sent if s.startswith('INVITE sip:09123456789@example.test'))
            side_id = side_invite.split('Call-ID: ', 1)[1].split('\r\n', 1)[0]
            self.assertIn(f'Refer-To: <sip:+919123456789@example.test;user=phone;method=INVITE?Replaces={side_id.replace("@", "%40")}', merge)
            self.assertIn('Referred-By: <sip:alice@example.test>', merge)
            # The conference leg was held while calling, then resumed.
            conf_reinvites = [s for s in reg.sent if s.startswith('INVITE') and f'Call-ID: {conf.sip_id}' in s and 'tag=remote-tag' in s.split('To: ', 1)[1].split('\r\n', 1)[0]]
            self.assertTrue(any('a=sendonly' in s for s in conf_reinvites))
            self.assertTrue(any('a=sendrecv' in s for s in conf_reinvites))
            self.assertEqual([p['number'] for p in conf.public()['participants']], ['9876543210', '9123456789'])

            # Progress reports (implicit REFER subscription NOTIFYs) update each person.
            seqs = sorted(conf.refers)
            await calls.incoming('NOTIFY sip:alice@[::1]:25000 SIP/2.0',
                                 in_dialog(conf, 'NOTIFY', 1, {'event': [f'refer;id={seqs[0]}']}), b'SIP/2.0 200 OK\r\n')
            self.assertTrue(reg.sent[-1].startswith('SIP/2.0 200 OK'))
            await calls.incoming('NOTIFY sip:alice@[::1]:25000 SIP/2.0',
                                 in_dialog(conf, 'NOTIFY', 2, {'event': [f'refer;id={seqs[1]}']}), b'SIP/2.0 200 OK\r\n')
            self.assertEqual([p['status'] for p in conf.public()['participants']], ['joined', 'joined'])

            # The first person's old call is replaced: the network ends it, the conference stays.
            await calls.incoming('BYE sip:alice@[::1]:25000 SIP/2.0', in_dialog(call, 'BYE', 9), b'')
            self.assertEqual(call.state, 'ended')
            self.assertEqual(conf.state, 'active')
            self.assertIs(calls.current, conf)
            await calls.hangup(conf)
            self.assertEqual(conf.state, 'ended')
        finally:
            await calls.close()

    async def test_busy_person_is_reported_and_retry_replaces_entry(self):
        reg, calls, call = await self.connected_call(busy=('09123456789',))
        try:
            conf = await calls.add_participant(call, '9123456789')
            await eventually(lambda: conf.adding is None)
            self.assertEqual(conf.public()['participants'][1], {'number': '9123456789', 'status': 'failed', 'error': 'The number is busy'})
            self.assertEqual(conf.state, 'active')
            self.assertIsNotNone(conf.browser_track is None or conf.peer is None)  # audio returned to the conference leg
            again = await calls.add_participant(conf, '9123456789')
            await eventually(lambda: again.adding is None)
            self.assertEqual(len(conf.participants), 2)  # the failed attempt was replaced, not duplicated
            await calls.hangup(conf)
        finally:
            await calls.close()

    async def test_failed_move_returns_to_the_original_call(self):
        reg, calls, call = await self.connected_call()
        try:
            conf = await calls.add_participant(call, '9123456789')
            first_refer = sorted(conf.refers)[0]
            await calls.incoming('NOTIFY sip:alice@[::1]:25000 SIP/2.0',
                                 in_dialog(conf, 'NOTIFY', 1, {'event': [f'refer;id={first_refer}']}), b'SIP/2.0 603 Decline\r\n')
            await eventually(lambda: conf.state == 'ended')
            self.assertIs(calls.current, call)
            self.assertEqual(call.state, 'active')
            self.assertIn('conference', call.error)
            await calls.hangup(call)
        finally:
            await calls.close()

    async def test_network_identity_is_tried_first_then_hold_and_retry(self):
        reg, calls, call = await self.connected_call(refer_failures=2)
        call.remote_identity = 'sip:+919876543210@ims.example.test;user=phone'
        try:
            conf = await calls.add_participant(call, '9123456789')
            refers = [s for s in reg.sent if s.startswith('REFER')]
            targets = [r.split('Refer-To: <', 1)[1].split('?Replaces', 1)[0] for r in refers[:3]]
            # 1) Jio-style address (500), 2) network identity (500), 3) dialog URI (202).
            self.assertEqual(targets, ['sip:+919876543210@example.test;user=phone;method=INVITE',
                                       'sip:+919876543210@ims.example.test;user=phone', 'sip:bob@example.test'])
            self.assertFalse(any(s.startswith('INVITE') and 'a=sendonly' in s for s in reg.sent))  # no hold needed
            self.assertIs(calls.current, conf)
            self.assertEqual(conf.participants[0]['status'], 'joining')
            await calls.hangup(conf)
        finally:
            await calls.close()

    async def test_hold_is_the_last_resort(self):
        reg, calls, call = await self.connected_call(refer_failures=3)
        call.remote_identity = 'sip:+919876543210@ims.example.test;user=phone'
        try:
            conf = await calls.add_participant(call, '9123456789')
            refers = [s for s in reg.sent if s.startswith('REFER')]
            hold = next(s for s in reg.sent if s.startswith('INVITE') and 'a=sendonly' in s)
            self.assertIn(f'Call-ID: {call.sip_id}', hold)
            self.assertLess(reg.sent.index(refers[2]), reg.sent.index(hold))
            self.assertIn(';user=phone;method=INVITE?Replaces=', refers[3])  # Jio-style again after the hold
            self.assertIs(calls.current, conf)
            await calls.hangup(conf)
        finally:
            await calls.close()

    def test_international_numbers(self):
        calls = Calls(ConferenceRouter(), FakeMedia)
        self.assertEqual(calls.international(number='9876543210'), '+919876543210')
        self.assertEqual(calls.international(number='09876543210'), '+919876543210')
        self.assertEqual(calls.international(number='+919876543210'), '+919876543210')
        self.assertEqual(calls.international(number='919876543210'), '+919876543210')

    def test_network_identity_prefers_sip_uri(self):
        headers = {'p-asserted-identity': ['<tel:+919876543210>', '<sip:+919876543210@ims.example.test;user=phone>']}
        self.assertEqual(network_identity(headers), 'sip:+919876543210@ims.example.test;user=phone')
        self.assertEqual(network_identity({'p-asserted-identity': ['<tel:+911234567890>']}), 'tel:+911234567890')
        self.assertEqual(network_identity({}), '')

    async def test_conference_rules(self):
        reg, calls, call = await self.connected_call()
        try:
            with self.assertRaises(ValueError):
                await calls.add_participant(call, '12')
            reg.config['conferenceFactory'] = 'sip:conf@factory.example.test'
            self.assertEqual(calls.factory_uri(), 'sip:conf@factory.example.test')
            await calls.hangup(call)
            with self.assertRaises(ValueError):
                await calls.add_participant(call, '9123456789')
        finally:
            await calls.close()


if __name__ == '__main__':
    unittest.main()
