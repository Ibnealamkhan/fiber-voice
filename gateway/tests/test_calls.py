import asyncio
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from calls import Calls, number_uri, failure_reason
from registration import parse_headers, first
from test_media import SDP

class FakeWriter:
    def get_extra_info(self,name):return ('::1',25000,0,0)
class FakeMedia:
    def __init__(self,address):self.remote=None;self.closed=False;self.track=None
    async def open(self):return self
    def sdp(self,answer=False,direction=None):return SDP+("a="+direction+"\r\n" if direction else "")
    def negotiate(self,sdp,incoming=False):self.remote={'codec':'AMR-WB'}
    def stats(self):return {'codec':'AMR-WB'}
    async def close(self):self.closed=True

class FakeRegistration:
    def __init__(self,scenario='answer'):
        self.config={'domain':'example.test','publicIdentity':'sip:alice@example.test','username':'alice','password':'secret','realm':'example.test'}
        self.contact='<sip:alice@[::1]:25000>;'+ '+sip.instance="<issued-value>"'
        self.writer=FakeWriter();self.scenario=scenario;self.sent=[];self.invite=None
    def status(self):return {'registered':True}
    def reply(self,method,seq,code,extra=(),body=''):
        call=self.call_handler.current
        raw=(f'SIP/2.0 {code} Result\r\nCall-ID: {call.sip_id}\r\nCSeq: {seq} {method}\r\nTo: <sip:bob@example.test>;tag=remote-tag\r\n'
             'Contact: <sip:remote@router.example.test;transport=tls>\r\nRecord-Route: <sip:second.example.test;lr>\r\nRecord-Route: <sip:first.example.test;lr>\r\n'+'\r\n'.join(extra)+'\r\nContent-Length: '+str(len(body.encode()))+'\r\n\r\n'+body)
        start,headers=parse_headers(raw.split('\r\n\r\n')[0].encode())
        asyncio.create_task(self.call_handler.response(start,headers,body.encode()))
    async def _send(self,message):
        self.sent.append(message)
        if message.startswith('SIP/'):return
        start,headers=parse_headers(message.split('\r\n\r\n')[0].encode())
        method=start.split()[0];seq=int(first(headers,'cseq').split()[0])
        if method=='INVITE':
            self.invite=seq
            if self.scenario=='fail':self.reply(method,seq,486);return
            if 'authorization' not in headers:
                self.reply(method,seq,401,['WWW-Authenticate: Digest realm="example.test", nonce="nonce", algorithm=MD5, qop="auth"']);return
            if self.scenario in ('cancel','race'):self.reply(method,seq,180);return
            self.reply(method,seq,183,['RSeq: 1','Require: 100rel'],SDP)
        elif method=='PRACK':
            self.reply(method,seq,200);self.reply('INVITE',self.invite,200,body=SDP)
        elif method=='CANCEL':
            self.reply(method,seq,200)
            self.reply('INVITE',self.invite,200 if self.scenario=='race' else 487,body=SDP if self.scenario=='race' else '')
        elif method in ('BYE','UPDATE'):self.reply(method,seq,200)

async def eventually(predicate):
    for _ in range(100):
        if predicate():return
        await asyncio.sleep(.005)
    raise AssertionError('State did not arrive')

class CallTests(unittest.IsolatedAsyncioTestCase):
    async def setup_call(self,scenario):
        reg=FakeRegistration(scenario);calls=Calls(reg,FakeMedia)
        call=await calls.prepare_outgoing('9876543210');call.invite_started=True
        call.spawn(calls.invite(call))
        return reg,calls,call

    async def test_digest_reliable_early_media_ack_and_bye(self):
        reg,calls,call=await self.setup_call('answer')
        try:
            await eventually(lambda:call.state=='active')
            prack=next(s for s in reg.sent if s.startswith('PRACK'))
            self.assertIn(f'RAck: 1 {call.invite_cseq} INVITE',prack)
            self.assertIn('tag=remote-tag',prack)
            await calls.hangup(call)
            self.assertEqual(call.state,'ended');self.assertTrue(call.media.closed)
            bye=next(s for s in reg.sent if s.startswith('BYE'))
            self.assertTrue(bye.startswith('BYE sip:remote@router.example.test;transport=tls'))
            self.assertLess(bye.index('first.example'),bye.index('second.example'))
            self.assertNotIn('secret',str(call.public()))
        finally:await calls.close()

    async def test_requests_while_ringing_are_accepted_not_481(self):
        reg,calls,call=await self.setup_call('cancel')
        try:
            await eventually(lambda:call.state=='ringing')
            base={'via':['SIP/2.0/TLS router.example.test;branch=z9hG4bKupd'],'from':['<sip:bob@example.test>;tag=remote-tag'],
                  'to':[call.local_from],'call-id':[call.sip_id],'contact':['<sip:remote@router.example.test>']}
            await calls.incoming('UPDATE sip:alice@[::1]:25000 SIP/2.0',{**base,'cseq':['1 UPDATE']},SDP.encode())
            reply=reg.sent[-1]
            self.assertTrue(reply.startswith('SIP/2.0 200 OK'),reply.split('\r\n')[0])
            self.assertIn('application/sdp',reply)
            self.assertEqual(call.state,'early_media')
            await calls.incoming('INFO sip:alice@[::1]:25000 SIP/2.0',{**base,'cseq':['2 INFO']},b'')
            self.assertTrue(reg.sent[-1].startswith('SIP/2.0 200 OK'))
            await calls.incoming('UPDATE sip:alice@[::1]:25000 SIP/2.0',{**base,'from':['<sip:bob@example.test>;tag=stranger'],'cseq':['3 UPDATE']},b'')
            self.assertTrue(reg.sent[-1].startswith('SIP/2.0 481'))
            self.assertTrue(any(line.startswith('← UPDATE') for line in call.trace))
            await calls.hangup(call)
        finally:await calls.close()

    async def test_cancel_preserves_original_branch_and_invite_sequence(self):
        reg,calls,call=await self.setup_call('cancel')
        try:
            await eventually(lambda:call.state=='ringing')
            await calls.hangup(call)
            await eventually(lambda:call.state=='cancelled')
            cancel=next(s for s in reg.sent if s.startswith('CANCEL'))
            self.assertIn('branch='+call.invite_branch,cancel)
            self.assertIn(f'CSeq: {call.invite_cseq} CANCEL',cancel)
            self.assertTrue(call.media.closed)
        finally:await calls.close()

    async def test_answer_racing_cancel_is_acked_and_bye_sent(self):
        reg,calls,call=await self.setup_call('race')
        try:
            await eventually(lambda:call.state=='ringing')
            await calls.hangup(call)
            await eventually(lambda:call.state=='cancelled')
            self.assertTrue(any(s.startswith('BYE') for s in reg.sent))
            self.assertTrue(any(s.startswith('ACK sip:remote') for s in reg.sent))
        finally:await calls.close()

    async def test_busy_response_is_acked_and_resources_released(self):
        reg,calls,call=await self.setup_call('fail')
        await eventually(lambda:call.state=='failed')
        self.assertEqual(call.last_sip,486);self.assertTrue(call.media.closed)
        self.assertTrue(any(s.startswith('ACK') for s in reg.sent))
        await calls.close()

    def test_number_validation_and_native_normalization(self):
        self.assertEqual(number_uri('98765 43210','example.test'),'sip:09876543210@example.test')
        for number in ['bob@host','1\r\nBad: yes','sip:123@host','../../']:
            with self.assertRaises(ValueError):number_uri(number,'example.test')

class FailureReasonTests(unittest.TestCase):
    def test_q850_cause_wins_over_sip_code(self):
        message,cause,raw=failure_reason(480,{'reason':['SIP;cause=480','Q.850;cause=20;text="Subscriber absent"']})
        self.assertEqual((message,cause),('The phone is switched off or out of coverage',20))
        self.assertIn('Subscriber absent',raw)
        self.assertEqual(failure_reason(480,{})[0],'The person is unavailable right now (phone off or out of coverage)')
        self.assertEqual(failure_reason(404,{'warning':['399 ims "no such user"']})[0],'This number does not exist')
        self.assertEqual(failure_reason(499,{})[0],'The call could not be connected')


class IncomingAndLateTests(unittest.IsolatedAsyncioTestCase):
    async def incoming_call(self):
        reg=FakeRegistration();calls=Calls(reg,FakeMedia)
        headers={'via':['SIP/2.0/TLS router.example.test;branch=z9hG4bKremote'],
                 'from':['<sip:bob@example.test>;tag=caller'], 'to':['<sip:alice@example.test>'],
                 'call-id':['incoming-call'], 'cseq':['1 INVITE'], 'contact':['<sip:bob@router.example.test>']}
        await calls.incoming('INVITE sip:alice@example.test SIP/2.0',headers,SDP.encode())
        await eventually(lambda:any(s.startswith('SIP/2.0 180') for s in reg.sent))
        return reg,calls,calls.current,headers

    async def test_incoming_answer_ack_and_remote_bye(self):
        reg,calls,call,headers=await self.incoming_call()
        try:
            self.assertIsNone(call.peer)
            await calls.answer(call)
            self.assertEqual(call.state,'answering')
            dialog={**headers,'to':[f'<sip:alice@example.test>;tag={call.local_tag}'],'cseq':['1 ACK']}
            await calls.incoming('ACK sip:alice@example.test SIP/2.0',dialog,b'')
            self.assertEqual(call.state,'active')
            await calls.incoming('BYE sip:alice@example.test SIP/2.0',{**dialog,'cseq':['2 BYE']},b'')
            self.assertEqual(call.state,'ended');self.assertTrue(call.media.closed)
        finally:await calls.close()

    async def test_incoming_cancel_closes_without_answering(self):
        reg,calls,call,headers=await self.incoming_call()
        await calls.incoming('CANCEL sip:alice@example.test SIP/2.0',{**headers,'cseq':['1 CANCEL']},b'')
        self.assertEqual(call.state,'cancelled');self.assertTrue(call.media.closed)
        self.assertTrue(any(s.startswith('SIP/2.0 487') for s in reg.sent))
        self.assertIsNone(call.peer)
        await calls.close()

    async def test_late_answer_of_cancelled_dialog_is_not_orphaned(self):
        reg=FakeRegistration('cancel');calls=Calls(reg,FakeMedia)
        old=await calls.prepare_outgoing('9876543210');old.invite_started=True;old.spawn(calls.invite(old))
        await eventually(lambda:old.state=='ringing')
        await calls.hangup(old);await eventually(lambda:old.state=='cancelled')
        new=await calls.prepare_outgoing('1234567890')
        headers={'call-id':[old.sip_id],'cseq':[f'{old.invite_cseq} INVITE'],
                 'to':['<sip:bob@example.test>;tag=late-answer'],'contact':['<sip:late@router.example.test>']}
        await calls.response('SIP/2.0 200 OK',headers,SDP.encode())
        await eventually(lambda:any(s.startswith('BYE sip:late@') for s in reg.sent))
        ack=next(s for s in reg.sent if s.startswith('ACK sip:late@'))
        self.assertIn('Call-ID: '+old.sip_id,ack)
        self.assertIs(calls.current,new);self.assertEqual(new.state,'dialing')
        await calls.close()
