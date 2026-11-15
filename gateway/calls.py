"""Single-call SIP dialog controller over the gateway's authenticated TLS flow."""
import asyncio
import re
from urllib.parse import quote
import secrets
import time
from dataclasses import dataclass, field

from media import MediaSession, codec_capabilities
from video import VideoSession, parse_video
from registration import first, digest_authorization, digest_parameters

LIVE = {'dialing','ringing','early_media','incoming','answering','active','ending'}


def number_uri(number, domain):
    number = re.sub(r'[ ()-]', '', str(number))
    if not re.fullmatch(r'\+?[0-9]{3,15}', number):
        raise ValueError('Enter a valid telephone number')
    # Match the native app's normalization of ten-digit destinations.
    if len(number)==10 and not number.startswith('0'):number='0'+number
    return 'sip:'+number+'@'+domain


# Why the network refused a call: ISDN/Q.850 causes from the Reason header, then the SIP code.
Q850_TEXT={1:'This number does not exist',3:'This number cannot be reached from your line',
           17:'The number is busy',18:'No answer',19:'No answer',
           20:'The phone is switched off or out of coverage',21:'The call was rejected',22:'This number has changed',
           27:'The phone is out of service',28:'The number is incomplete or invalid',
           34:'The network is busy, try again',38:'Network problem, try again',41:'Network problem, try again',
           42:'The network is busy, try again',47:'Network problem, try again',
           57:'Your line is not allowed to call this number',63:'Calling this number is not available on your line',
           102:'No answer'}
SIP_TEXT={403:'Your line is not allowed to call this number',404:'This number does not exist',408:'No answer',
          480:'The person is unavailable right now (phone off or out of coverage)',
          484:'The number is incomplete or invalid',486:'The number is busy',488:'The other side could not accept the call',
          500:'Network problem, try again',503:'The network is busy, try again',600:'The number is busy',603:'The call was declined'}


def failure_reason(code,headers):
    """(message, Q.850 cause or None, raw Reason/Warning headers for the log)."""
    cause=None
    for value in headers.get('reason',[]):
        match=re.match(r'\s*Q\.850\s*;.*?cause\s*=\s*(\d+)',value,re.I)
        if match:cause=int(match[1]);break
    message=Q850_TEXT.get(cause) or SIP_TEXT.get(code) or 'The call could not be connected'
    raw='; '.join(headers.get('reason',[])+headers.get('warning',[]))
    return message,cause,raw


TRACE_HEADERS=('reason','warning','server','retry-after','p-asserted-identity','require','supported','allow','contact','content-type')


def masked(text):
    """Hide phone numbers in diagnostic logs, keeping the last 4 digits."""
    return re.sub(r'\+?\d{6,}',lambda m:('+' if m[0][0]=='+' else '')+'…'+m[0][-4:]+f'({len(m[0].lstrip("+"))} digits)',text)


def network_identity(headers):
    """The party's network identity from P-Asserted-Identity, preferring a SIP URI (it can carry ?Replaces)."""
    uris=[]
    for value in headers.get('p-asserted-identity',[]):
        for match in re.finditer(r'<([^>]+)>',value) if '<' in value else [re.match(r'\s*(\S+)',value)]:
            if match and re.fullmatch(r'(sips?|tel):[^\s<>]+',match[1]):uris.append(match[1])
    return next((u for u in uris if u.startswith('sip')),uris[0] if uris else '')


def uri_from(value):
    match=re.search(r'<([^>]+)>',value)
    uri=match[1] if match else value.split(';tag=',1)[0].strip()
    if not re.fullmatch(r'sips?:[^\s<>\r\n]+',uri):raise ValueError('Invalid SIP dialog target')
    return uri


def tag_from(value):
    match=re.search(r'(?:^|;)\s*tag=([^;\s]+)',value)
    return match[1] if match else ''


def response_message(headers,code,reason,tag=None,extra=(),body=''):
    to=first(headers,'to')
    if tag and not tag_from(to):to+=';tag='+tag
    lines=[f'SIP/2.0 {code} {reason}']+['Via: '+v for v in headers.get('via',[])]
    lines += ['From: '+first(headers,'from'),'To: '+to,'Call-ID: '+first(headers,'call-id'),'CSeq: '+first(headers,'cseq')]
    lines += list(extra)
    if body:lines += ['Content-Type: application/sdp']
    lines += ['Content-Length: '+str(len(body.encode())),'','']
    return '\r\n'.join(lines)+body


@dataclass
class Call:
    id: str
    sip_id: str
    direction: str
    number: str
    target: str
    local_tag: str=field(default_factory=lambda:secrets.token_hex(10))
    state: str='dialing'
    error: str|None=None
    cseq: int=0
    invite_cseq: int=0
    invite_branch: str=''
    local_from: str=''
    remote_to: str=''
    remote_tag: str=''
    routes: list=field(default_factory=list)
    media: object=None
    peer: object=None
    transactions: dict=field(default_factory=dict)
    tasks: set=field(default_factory=set)
    auth: object=None
    nonce_count: int=0
    provisional: bool=False
    cancel_requested: bool=False
    cancel_sent: bool=False
    last_response: str|None=None
    incoming_headers: dict|None=None
    answered_sdp: str|None=None
    rseqs: set=field(default_factory=set)
    ack_event: object=field(default_factory=asyncio.Event)
    created: float=field(default_factory=time.time)
    connected: float|None=None
    ended: float|None=None
    last_sip: int|None=None
    bye_started: bool=False
    user_declined: bool=False
    failure_cause: int|None=None
    trace: list=field(default_factory=list)
    early_tags: set=field(default_factory=set)
    video: bool=False
    offered_video: bool=False
    conference: bool=False
    participants: list=field(default_factory=list)
    refers: dict=field(default_factory=dict)
    previous: object=None
    remote_identity: str=''
    adding: object=None
    browser_track: object=None
    attach_task: object=None
    screened: str|None=None

    def spawn(self,coroutine):
        task=asyncio.create_task(coroutine);self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def public(self):
        return {'id':self.id,'direction':self.direction,'number':self.number,'state':self.state,
                'error':self.error,'sipCode':self.last_sip,'createdAt':self.created,
                'connectedAt':self.connected,'endedAt':self.ended,
                'videoRequested':self.video,'offeredVideo':self.offered_video,
                'conference':self.conference,'adding':bool(self.adding and self.adding.state in LIVE),
                'participants':[{k:p.get(k) for k in ('number','status','error')} for p in self.participants],
                'videoActive':bool(self.media and getattr(self.media,'video',None) and self.media.video.active),
                'media':self.media.stats() if self.media else None}


class Calls:
    def __init__(self,registration,media_factory=MediaSession,history=None,screening=None):
        self.registration=registration
        self.history=history
        self.screening=screening
        self.media_factory=media_factory
        self.current=None
        self.dialogs={}
        self.background=set()
        self.lock=asyncio.Lock()
        self.conference_lock=asyncio.Lock()
        self.capabilities=codec_capabilities()
        registration.call_handler=self

    def status(self):
        return {'available':self.registration.status()['registered'] and self.capabilities['ready'],
                'codecs':self.capabilities,'call':self.current.public() if self.current else None}

    async def new_media(self,video=False):
        local=self.registration.writer.get_extra_info('sockname')[0]
        media=self.media_factory(local)
        try:
            await media.open()
            if video:media.video=await VideoSession(local).open()
            return media
        except BaseException:
            await media.close()
            raise

    def contact(self,call):
        # Video calls carry the RFC 3840 video feature tag, as Jio's own client does.
        return self.registration.contact+(';video' if call.video else '')

    async def prepare_outgoing(self,number,video=False):
        async with self.lock:
            if not self.status()['available']:raise ValueError('Registration and native AMR codecs must be ready')
            if self.current and self.current.state in LIVE:raise ValueError('Another call is in progress')
            call=self.outgoing_call(number)
            call.video=bool(video)
            call.media=await self.new_media(call.video)
            call.media.on_error=lambda error:call.spawn(self.media_failed(call,error))
            self.current=call
            self.remember(call)
            return call

    def outgoing_call(self,number):
        uri=number_uri(number,self.registration.config['domain'])
        call=Call(secrets.token_urlsafe(18),secrets.token_hex(16)+'@fiber-gateway','outgoing',str(number),uri)
        call.local_from=f'<{self.registration.config["publicIdentity"]}>;tag={call.local_tag}'
        call.remote_to='<'+uri+'>'
        return call

    def remember(self,call):
        self.dialogs[call.sip_id]=call
        # Retain completed transactions long enough to ACK late/retransmitted 2xx.
        for key,value in list(self.dialogs.items()):
            if value.ended and time.time()-value.ended>120:self.dialogs.pop(key,None)

    def spawn(self,coroutine):
        task=asyncio.create_task(coroutine);self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    def route(self,call,initial=False):
        if initial or not call.routes:return call.target,[]
        routes=list(call.routes)
        if ';lr' not in routes[0].lower():
            target=uri_from(routes.pop(0));routes.append('<'+call.target+'>')
            return target,routes
        return call.target,routes

    def request(self,call,method,body='',initial=False,extra=(),sequence=None,branch=None,authenticate=True,dialog=None):
        if sequence is None:call.cseq+=1;sequence=call.cseq
        branch=branch or 'z9hG4bK'+secrets.token_hex(12)
        target,routes=self.route(call,initial)
        to=call.remote_to
        if dialog:
            target,routes,to=dialog
            routes=list(routes)
            if routes and ';lr' not in routes[0].lower():
                original=target;target=uri_from(routes.pop(0));routes.append('<'+original+'>')
        local=self.registration.writer.get_extra_info('sockname')
        lines=[f'{method} {target} SIP/2.0',
               f'Via: SIP/2.0/TLS [{local[0]}]:{local[1]};branch={branch};rport',
               'Max-Forwards: 70','From: '+call.local_from,'To: '+to,
               'Call-ID: '+call.sip_id,f'CSeq: {sequence} {method}']
        lines += ['Route: '+r for r in routes]
        if method not in ('ACK','CANCEL'):lines += ['Contact: '+self.contact(call)]
        lines += ['User-Agent: FiberGateway/0.2','X-Client: JSE']
        if method=='INVITE':
            lines += ['Supported: 100rel, timer, replaces','Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, PRACK, UPDATE, INFO',
                      'P-Preferred-Identity: <'+self.registration.config['publicIdentity']+'>',
                      'P-Preferred-Service: urn:urn-7:3gpp-service.ims.icsi.mmtel',
                      *(['Accept-Contact: *;+g.3gpp.icsi-ref="urn%3Aurn-7%3A3gpp-service.ims.icsi.mmtel";video'] if call.video else []),
                      'Session-Expires: 1800;refresher=uac','Min-SE: 90']
        if call.auth and authenticate and method not in ('ACK','CANCEL'):
            name,challenge=call.auth;call.nonce_count+=1
            cfg=self.registration.config
            auth=digest_authorization(challenge,cfg['username'],cfg['password'],method,target,call.nonce_count,body=body)
            lines.append(name+': '+auth)
        lines += list(extra)
        if body:lines+=['Content-Type: application/sdp']
        lines += ['Content-Length: '+str(len(body.encode())),'','']
        return '\r\n'.join(lines)+body,sequence,branch

    async def send_transaction(self,call,method,body='',initial=False,extra=(),timeout=12,sequence=None,branch=None,dialog=None):
        message,seq,branch=self.request(call,method,body,initial,extra,sequence,branch,dialog=dialog)
        queue=asyncio.Queue()
        call.transactions[(seq,method)]=queue
        if method=='INVITE':call.invite_cseq=seq;call.invite_branch=branch
        try:
            await self.registration._send(message)
            end=asyncio.get_running_loop().time()+timeout
            while True:
                start,headers,raw=await asyncio.wait_for(queue.get(),max(.1,end-asyncio.get_running_loop().time()))
                code=int(start.split()[1]);call.last_sip=code
                if code<200:
                    await self.provisional(call,code,headers,raw)
                    continue
                return code,headers,raw
        finally:
            call.transactions.pop((seq,method),None)
            while not queue.empty():
                start,headers,raw=queue.get_nowait()
                if method=='INVITE' and start.startswith('SIP/2.0 2'):
                    self.spawn(self.response(start,headers,raw))

    async def authenticated(self,call,method,body='',initial=False,extra=(),timeout=12,dialog=None):
        for attempt in range(3):
            code,headers,raw=await self.send_transaction(call,method,body,initial,extra,timeout,dialog=dialog)
            if code in (401,407):
                if method=='INVITE':await self.ack_failure(call,headers)
                challenge=first(headers,'www-authenticate' if code==401 else 'proxy-authenticate')
                params=digest_parameters(challenge)
                if params.get('realm')!=self.registration.config['realm']:raise ValueError('Call authentication realm differs from provisioning')
                if attempt and params.get('stale','').lower()!='true':raise ValueError('Call authentication rejected')
                call.auth=('Authorization' if code==401 else 'Proxy-Authorization',challenge);call.nonce_count=0
                continue
            return code,headers,raw
        raise ValueError('Call authentication negotiation failed')

    async def invite(self,call):
        try:
            offer=call.media.sdp()
            code,headers,body=await self.authenticated(call,'INVITE',offer,True,timeout=65)
            if code>=300:
                await self.ack_failure(call,headers)
                if call.cancel_requested and code==487:
                    await self.finish(call,'cancelled');return
                message,cause,raw=failure_reason(code,headers)
                call.failure_cause=cause
                # Diagnostic log (gateway.log): never the full number.
                print(f'Outgoing call to …{call.number[-4:]} refused: SIP {code}'+(f', Q.850 cause {cause}' if cause else '')+(f' [{raw}]' if raw else ''),flush=True)
                print(f'  dialled: {masked(call.target)}',flush=True)
                for line in call.trace:print('  '+line,flush=True)
                await self.finish(call,'failed',f'{message} (SIP {code}'+(f', cause {cause}' if cause else '')+')')
                return
            call.remote_to=first(headers,'to',call.remote_to);call.remote_tag=tag_from(call.remote_to)
            call.remote_identity=network_identity(headers) or call.remote_identity
            call.target=uri_from(first(headers,'contact',call.target))
            call.routes=list(reversed(headers.get('record-route',[])))
            # ACK even when the SDP is unsupported, then tear down the accepted dialog.
            ack,_,_=self.request(call,'ACK',sequence=call.invite_cseq,authenticate=False)
            await self.registration._send(ack)
            if call.cancel_requested or call.ended:
                await self.bye(call);await self.finish(call,'cancelled');return
            if body:
                try:call.media.negotiate(body.decode())
                except Exception:
                    await self.bye(call)
                    await self.finish(call,'failed','Router answered with unsupported audio')
                    return
            if not call.media.remote:
                await self.bye(call);await self.finish(call,'failed','Router answered without audio negotiation');return
            if call.cancel_requested:
                await self.bye(call);await self.finish(call,'cancelled');return
            call.state='active';call.connected=time.time()
            if call.video:
                video=getattr(call.media,'video',None)
                remote=video.remote if video else None
                print(f'Video call to …{call.number[-4:]} connected: '+(f"network video {remote['width']}x{remote['height']} H.264 pt {remote['pt']} {remote['direction']}" if remote else 'network declined video (voice only)'),flush=True)
                if not remote:
                    for line in call.trace:print('  '+line,flush=True)
            self.schedule_session_timer(call,headers)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if call.state in LIVE:
                # Diagnostic log (gateway.log): what the network sent before setup gave up.
                print(f'Outgoing call to …{call.number[-4:]} failed: {str(error) or "no final answer within 65 s"} (state {call.state})',flush=True)
                print(f'  dialled: {masked(call.target)}',flush=True)
                for line in call.trace:print('  '+line,flush=True)
                if call.provisional and not call.remote_tag:
                    try:await self.send_cancel(call)
                    except Exception:pass
                await self.finish(call,'failed',str(error) or 'Call setup timed out')

    async def ack_failure(self,call,headers):
        old=call.remote_to;call.remote_to=first(headers,'to',old)
        message,_,_=self.request(call,'ACK',initial=True,sequence=call.invite_cseq,branch=call.invite_branch,authenticate=False)
        call.remote_to=old
        await self.registration._send(message)

    async def provisional(self,call,code,headers,body):
        call.provisional=True
        # The callee side's tag identifies the early dialog; the network may send
        # UPDATE/INFO/OPTIONS inside it while the phone is still ringing.
        tag=tag_from(first(headers,'to'))
        if tag:call.early_tags.add(tag)
        if not call.remote_identity:call.remote_identity=network_identity(headers)
        if code==180 and not call.cancel_requested:call.state='ringing'
        if code in (180,183) and body and not call.cancel_requested:
            try:call.media.negotiate(body.decode());call.state='early_media'
            except ValueError:pass
        rseq=first(headers,'rseq')
        if rseq and rseq not in call.rseqs:
            if not rseq.isdigit():raise ValueError('Invalid reliable provisional sequence')
            call.rseqs.add(rseq)
            call.spawn(self.prack(call,headers,rseq))
        if call.cancel_requested and not call.cancel_sent:call.spawn(self.send_cancel(call))

    async def prack(self,call,headers,rseq):
        early_to=first(headers,'to',call.remote_to)
        try:
            target=uri_from(first(headers,'contact',call.target))
            routes=list(reversed(headers.get('record-route',[])))
            code,_,_=await self.authenticated(call,'PRACK',extra=[f'RAck: {rseq} {call.invite_cseq} INVITE'],dialog=(target,routes,early_to))
            if not 200<=code<300:raise ValueError('Reliable ringing acknowledgement rejected')
        except Exception:
            if call.state in LIVE:await self.hangup(call)

    async def response(self,start,headers,body):
        call=self.dialogs.get(first(headers,'call-id'))
        if not call:return False
        parts=first(headers,'cseq').split()
        if len(parts)!=2 or not parts[0].isdigit():return True
        key=(int(parts[0]),parts[1]);queue=call.transactions.get(key)
        if parts[1]=='INVITE' and len(call.trace)<30:
            # Short, number-masked trace of what the network answered (logged only if the call fails).
            extra=' | '.join(f'{name}: {value}' for name in TRACE_HEADERS for value in headers.get(name,[]))
            call.trace.append(masked(start+(f' | {extra}' if extra else '')+(f' | SDP {len(body)} bytes' if body else '')))
        if queue:
            queue.put_nowait((start,headers,body))
        elif parts[1]=='INVITE' and start.startswith('SIP/2.0 2') and call.direction=='outgoing':
            # A retransmitted 2xx must still receive its end-to-end ACK.
            if call.ended:print(f'Late answer for call to …{call.number[-4:]} (state {call.state}, CSeq {parts[0]}, waiting for {call.invite_cseq})',flush=True)
            remote_to=first(headers,'to')
            target=uri_from(first(headers,'contact',call.target))
            routes=list(reversed(headers.get('record-route',[])))
            dialog=(target,routes,remote_to)
            ack,_,_=self.request(call,'ACK',sequence=int(parts[0]),authenticate=False,dialog=dialog)
            await self.registration._send(ack)
            if tag_from(remote_to)!=call.remote_tag or (call.ended and not call.bye_started):
                async def terminate_late():
                    try:await self.authenticated(call,'BYE',timeout=5,dialog=dialog)
                    except Exception:pass
                self.spawn(terminate_late())
        return True

    async def incoming(self,start,headers,body):
        method=start.split()[0]
        sip_id=first(headers,'call-id')
        call=self.dialogs.get(sip_id)
        matched=call is not None
        if method=='INVITE' and not tag_from(first(headers,'to')):
            if matched:
                if call.last_response:await self.registration._send(call.last_response)
                return True
            if await self.screen(sip_id,headers):return True
            if self.current and self.current.state in LIVE:
                await self.registration._send(response_message(headers,486,'Busy Here'));return True
            # Claim the call synchronously before the asynchronous setup task runs.
            from_uri=uri_from(first(headers,'from'))
            number=from_uri.split(':',1)[1].split('@')[0]
            call=Call(secrets.token_urlsafe(18),sip_id,'incoming',number,from_uri,state='incoming')
            call.incoming_headers=headers;call.remote_to=first(headers,'from');call.remote_tag=tag_from(call.remote_to)
            call.remote_identity=network_identity(headers)
            call.local_from=first(headers,'to')+';tag='+call.local_tag
            call.target=uri_from(first(headers,'contact',from_uri));call.routes=headers.get('record-route',[])[:]
            call.invite_cseq=int(first(headers,'cseq').split()[0])
            self.current=call
            self.remember(call)
            await self.registration._send(response_message(headers,100,'Trying'))
            call.spawn(self.prepare_incoming(call,body))
            return True
        if matched and len(call.trace)<30:
            extra=' | '.join(f'{name}: {value}' for name in ('reason','content-type') for value in headers.get(name,[]))
            call.trace.append(masked(f'← {method} (state {call.state})'+(f' | {extra}' if extra else '')+(f' | SDP {len(body)} bytes' if body else '')))
        if not matched:
            if method in ('BYE','CANCEL','ACK','UPDATE','PRACK','INVITE'):
                if method!='ACK':await self.registration._send(response_message(headers,481,'Call Does Not Exist'))
                return True
            return False
        if method not in ('CANCEL',) and tag_from(first(headers,'to'))!=call.local_tag:
            if method!='ACK':await self.registration._send(response_message(headers,481,'Call Does Not Exist'))
            return True
        remote=tag_from(first(headers,'from'))
        early=call.direction=='outgoing' and not call.connected and remote in call.early_tags
        if method not in ('CANCEL',) and remote!=call.remote_tag and not early:
            if method!='ACK':await self.registration._send(response_message(headers,481,'Call Does Not Exist'))
            return True
        if method=='CANCEL':
            await self.registration._send(response_message(headers,200,'OK',call.local_tag))
            if call.direction=='incoming' and call.state=='incoming':
                await self.registration._send(response_message(call.incoming_headers,487,'Request Terminated',call.local_tag))
                await self.finish(call,'cancelled')
            return True
        if method=='ACK':
            if call.state=='answering':call.ack_event.set();call.state='active';call.connected=time.time()
            return True
        if method=='BYE':
            await self.registration._send(response_message(headers,200,'OK',call.local_tag))
            await self.finish(call,'ended');return True
        if method=='NOTIFY' and call.conference:
            self.refer_notify(call,headers,body)
            await self.registration._send(response_message(headers,200,'OK',call.local_tag));return True
        if method in ('INFO','OPTIONS','NOTIFY'):
            await self.registration._send(response_message(headers,200,'OK',call.local_tag));return True
        if method=='UPDATE' and early and call.state in ('dialing','ringing','early_media','ending'):
            # Session update before answer (e.g. codec or early media change): accept it.
            before=(call.media.remote or {}).get('codec')
            try:
                if body:call.media.negotiate(body.decode(),incoming=True);call.state='early_media' if call.state!='ending' else call.state
                response_body=call.media.sdp(answer=True) if body else ''
                reply=response_message(headers,200,'OK',call.local_tag,['Contact: '+self.registration.contact],response_body)
                after=(call.media.remote or {}).get('codec')
                call.trace.append(f'→ 200 OK to UPDATE'+(f' (audio {before} → {after})' if before!=after else f' ({after})'))
            except Exception as error:
                reply=response_message(headers,488,'Not Acceptable Here',call.local_tag)
                call.trace.append(masked(f'→ 488 to UPDATE: {error}'))
                print(masked(f'Call to …{call.number[-4:]}: early UPDATE rejected: {error}'),flush=True)
            await self.registration._send(reply);return True
        if method in ('UPDATE','INVITE'):
            if call.state not in ('active','answering'):
                await self.registration._send(response_message(headers,491,'Request Pending',call.local_tag));return True
            try:
                if body:call.media.negotiate(body.decode(),incoming=True)
                response_body=call.media.sdp(answer=True) if body else ''
                reply=response_message(headers,200,'OK',call.local_tag,['Contact: '+self.registration.contact],response_body)
            except Exception:reply=response_message(headers,488,'Not Acceptable Here',call.local_tag)
            await self.registration._send(reply);return True
        return False

    async def screen(self,sip_id,headers):
        """Reject blocked callers (603) and, during Do Not Disturb, stay silent (480)."""
        if not self.screening:return False
        try:
            from_uri=uri_from(first(headers,'from'))
            number=from_uri.split(':',1)[1].split('@')[0].split(';')[0]
            verdict=self.screening.decide(number)
        except Exception:return False
        if not verdict:return False
        code,reason=(603,'Decline') if verdict=='blocked' else (480,'Temporarily Unavailable')
        await self.registration._send(response_message(headers,code,reason,secrets.token_hex(10)))
        if self.history:
            record=Call(secrets.token_urlsafe(18),sip_id,'incoming',number,from_uri,state='declined',screened=verdict)
            record.ended=record.created
            try:self.history.add(record)
            except Exception:pass
        return True

    async def prepare_incoming(self,call,body):
        try:
            if not self.capabilities['ready'] or not body:raise ValueError('Incoming call requires an AMR SDP offer')
            call.offered_video=parse_video(body.decode()) is not None
            call.media=await self.new_media(call.offered_video)
            call.media.on_error=lambda error:call.spawn(self.media_failed(call,error))
            call.media.negotiate(body.decode(),incoming=True)
            call.last_response=response_message(call.incoming_headers,180,'Ringing',call.local_tag,['Contact: '+self.registration.contact])
            await self.registration._send(call.last_response)
            await asyncio.sleep(45)
            if call.state=='incoming':await self.reject(call)
        except asyncio.CancelledError:raise
        except Exception as error:
            await self.registration._send(response_message(call.incoming_headers,488,'Not Acceptable Here',call.local_tag))
            await self.finish(call,'failed',str(error))

    async def choose_video(self,call,with_video):
        """Before answering an incoming call: keep its offered video, or answer voice-only."""
        call.video=bool(with_video and call.offered_video and call.media.video)
        if not call.video and call.media.video:
            video,call.media.video=call.media.video,None
            await video.close()

    async def answer(self,call):
        if call is not self.current or call.state!='incoming' or not call.media:raise ValueError('No incoming call ready to answer')
        call.state='answering'
        call.answered_sdp=call.media.sdp(answer=True)
        call.last_response=response_message(call.incoming_headers,200,'OK',call.local_tag,['Contact: '+self.contact(call)],call.answered_sdp)
        await self.registration._send(call.last_response)
        call.spawn(self.wait_ack(call))

    async def wait_ack(self,call):
        delay=.5;end=asyncio.get_running_loop().time()+32
        while not call.ack_event.is_set() and call.state=='answering':
            try:await asyncio.wait_for(call.ack_event.wait(),delay)
            except asyncio.TimeoutError:
                if asyncio.get_running_loop().time()>end:
                    await self.finish(call,'failed','Caller did not acknowledge the answer');return
                await self.registration._send(call.last_response);delay=min(4,delay*2)

    async def reject(self,call):
        if call.state=='incoming':
            call.last_response=response_message(call.incoming_headers,486,'Busy Here',call.local_tag)
            await self.registration._send(call.last_response)
            await self.finish(call,'declined')

    async def send_cancel(self,call):
        if call.cancel_sent or not call.provisional:return
        call.cancel_sent=True
        message,seq,_=self.request(call,'CANCEL',initial=True,sequence=call.invite_cseq,branch=call.invite_branch,authenticate=False)
        await self.registration._send(message)

    async def bye(self,call):
        if call.bye_started:return
        call.bye_started=True
        try:
            await self.authenticated(call,'BYE',timeout=5)
        except Exception:pass

    async def hangup(self,call=None):
        call=call or self.current
        if not call or call.state not in LIVE:return
        if call.direction=='outgoing' and not getattr(call,'invite_started',False):
            await self.finish(call,'cancelled');return
        if call.direction=='incoming' and call.state=='incoming':call.user_declined=True;await self.reject(call);return
        if call.state in ('active','answering') or call.remote_tag and call.direction=='outgoing':
            call.state='ending';await self.bye(call);await self.finish(call,'ended');return
        call.cancel_requested=True;call.state='ending'
        await self.send_cancel(call)
        call.spawn(self.cancel_timeout(call))

    async def cancel_timeout(self,call):
        await asyncio.sleep(8)
        if call.state=='ending':await self.finish(call,'cancelled')

    def schedule_session_timer(self,call,headers):
        value=first(headers,'session-expires')
        if not value:return
        match=re.match(r'(\d+)',value)
        if not match:return
        seconds=int(match[1])
        if seconds<90:return
        if 'refresher=uas' in value.lower():return
        async def refresh():
            while call.state=='active':
                await asyncio.sleep(seconds*.5)
                if call.state!='active':return
                try:
                    code,_,_=await self.authenticated(call,'UPDATE',extra=[f'Session-Expires: {seconds};refresher=uac'])
                    if code!=200:raise ValueError('Call session refresh rejected')
                except Exception:
                    await self.hangup(call);return
        call.spawn(refresh())

    async def finish(self,call,state,error=None):
        if call.ended:return
        call.state=state;call.error=error;call.ended=time.time()
        if call.conference and call.adding and call.adding.state in LIVE:
            print(f'Conference ended ({state}) while …{call.adding.number[-4:]} was being added',flush=True)
            self.spawn(self.hangup(call.adding))
        if self.history and not call.conference:
            try:self.history.add(call)
            except Exception:pass
        current=asyncio.current_task()
        tasks=[t for t in call.tasks if t is not current]
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        # Stop the browser leg first so nothing asks the media for audio while it closes.
        if call.peer:await call.peer.close()
        if call.media:await call.media.close()

    async def disconnected(self):
        if self.current and self.current.state in LIVE:
            await self.finish(self.current,'failed','SIP connection lost')

    async def media_failed(self,call,error):
        if call.state not in LIVE:return
        if call.state=='active':await self.bye(call)
        elif call.direction=='outgoing':
            call.cancel_requested=True
            await self.send_cancel(call)
        await self.finish(call,'failed','Audio bridge failed: '+error)

    # ---- Conference calls (3GPP TS 24.147: conference factory + REFER) ----

    def factory_uri(self):
        cfg=self.registration.config
        return cfg.get('conferenceFactory') or f"sip:mmtel@conf-factory.{cfg['domain']}"

    async def move_audio(self,source,target):
        """Re-wire the browser's WebRTC audio from one SIP call's media to another's."""
        pc=source.peer
        target.peer,source.peer=pc,None
        if source.attach_task:source.attach_task.cancel()
        target.browser_track,source.browser_track=source.browser_track,None
        if target.browser_track:target.attach_task=target.media.spawn(target.media.attach(target.browser_track))
        if pc:
            for sender in pc.getSenders():
                if sender.track and sender.track.kind=='audio':sender.replaceTrack(target.media.track)

    def international(self,call=None,number=None):
        """+91… form of a party's number, from its network identity or the digits dialled."""
        for value in ((call.remote_identity,call.number,uri_from(call.remote_to)) if call else ())+((number,) if number else ()):
            digits=re.sub(r'[^\d+]','',str(value or '').split(':',1)[-1].split('@',1)[0].split(';',1)[0])
            bare=digits.lstrip('+')
            if digits.startswith('+') and len(bare)>=8:return digits
            if len(bare)==12 and bare.startswith('91'):return '+'+bare
            if len(bare)==11 and bare.startswith('0'):return '+91'+bare[1:]
            if len(bare)==10:return '+91'+bare
        return None

    def jio_target(self,number):
        # Exactly the Refer-To Jio's own client builds (VvmSession::initiateInDialogRefer):
        # tel:+91… -> sip:+91…@<home domain>;user=phone, then ;method=INVITE.
        return f"sip:{number}@{self.registration.config['domain']};user=phone;method=INVITE"

    def replaces_targets(self,call):
        """Refer-To candidates for moving `call` into a conference, best first."""
        # RFC 3891: to-tag is the other party's tag, from-tag is ours.
        replaces='?Replaces='+quote(f'{call.sip_id};to-tag={call.remote_tag};from-tag={call.local_tag}',safe='')
        number=self.international(call)
        uris=([self.jio_target(number)] if number else [])+[u for u in (call.remote_identity,uri_from(call.remote_to)) if u and u.startswith('sip')]
        return [u+replaces for u in dict.fromkeys(uris)]

    async def hold(self,call,direction='sendonly'):
        """Hold (a=sendonly) or resume (a=sendrecv) a call with a re-INVITE, as Jio's client does around merges."""
        body=call.media.sdp(direction=direction)
        code,headers,answer=await self.authenticated(call,'INVITE',body)
        seq=first(headers,'cseq').split()[0]
        if 200<=code<300 and seq.isdigit():
            ack,_,_=self.request(call,'ACK',sequence=int(seq),authenticate=False)
            await self.registration._send(ack)
            if answer:
                try:call.media.negotiate(answer.decode())
                except Exception:pass
        print(f"{'Hold' if direction=='sendonly' else 'Resume'} {'conference' if call.conference else 'call'}: SIP {code}",flush=True)
        return 200<=code<300

    async def add_participant(self,call,number):
        """Add someone to a connected call; the first time, turn the call into a network conference."""
        async with self.conference_lock:
            if call is not self.current or call.state!='active':raise ValueError('Add people during a connected call')
            if getattr(call.media,'video',None):raise ValueError('Conference calls are voice only')
            number_uri(number,self.registration.config['domain'])  # validates the number
            if call.conference and call.adding and call.adding.state in LIVE:
                raise ValueError('Wait until the person you are adding answers')
            conf=call if call.conference else await self.start_conference(call)
            if len([p for p in conf.participants if p['status']!='failed'])>=6:
                raise ValueError('A conference can have up to 6 people besides you')
            # Retrying a number replaces its earlier failed attempt.
            digits=re.sub(r'\D','',str(number))[-10:]
            conf.participants=[p for p in conf.participants if not (p['status']=='failed' and re.sub(r'\D','',p['number'])[-10:]==digits)]
            participant={'number':str(number),'status':'calling','error':None}
            conf.participants.append(participant)
            side=self.outgoing_call(number)
            side.media=await self.new_media()
            side.media.on_error=lambda error:side.spawn(self.media_failed(side,error))
            self.remember(side);conf.adding=side
            conf.spawn(self.dial_into_conference(conf,participant,side))
            return conf

    async def dial_into_conference(self,conf,participant,side):
        """Phone-style add: hold the conference, call the person, then merge their call in (REFER + Replaces).

        Jio's router only merges calls that already exist; it rejects a REFER that asks it to dial someone."""
        try:
            await self.hold(conf)
            await self.move_audio(conf,side)  # hear it ring and greet them
            side.invite_started=True
            watcher=asyncio.create_task(self.mirror_status(side,participant))
            print(f'Adding …{side.number[-4:]} to conference: calling',flush=True)
            await self.invite(side)
            watcher.cancel()
            print(f'Adding …{side.number[-4:]}: call {side.state}'+(f' ({side.error})' if side.error else '')+f', SIP {side.last_sip}',flush=True)
            for line in side.trace[-6:]:print('  '+line,flush=True)
            if side.state!='active':
                participant.update(status='failed',error=(side.error or ('Stopped' if side.cancel_requested else 'No answer')).split(' (SIP')[0])
                return
            participant['status']='joining'
            await self.move_audio(side,conf)
            print(f'Adding …{side.number[-4:]}: answered, merging into the conference',flush=True)
            if not await self.refer_existing(conf,participant,side):
                participant.update(status='failed',error='Could not merge them into the conference')
                await self.hangup(side)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            participant.update(status='failed',error=str(error) or 'Could not add them')
            if side.state in LIVE:await self.hangup(side)
        finally:
            if side.peer is not None and conf.state=='active':await self.move_audio(side,conf)
            conf.adding=None
            if conf.state=='active':
                try:await self.hold(conf,'sendrecv')
                except Exception:pass

    async def mirror_status(self,side,participant):
        while side.state in LIVE and participant['status'] in ('calling','ringing'):
            participant['status']='ringing' if side.state in ('ringing','early_media') else 'calling'
            await asyncio.sleep(0.25)

    async def refer_existing(self,conf,participant,call):
        """Move an existing call into the conference, trying Refer-To forms best first."""
        for target in self.replaces_targets(call):
            participant['status']='joining';participant['error']=None
            try:
                await self.refer(conf,participant,target)
                return True
            except Exception:
                continue
        return False

    async def cancel_add(self,conf):
        if conf.adding and conf.adding.state in LIVE:
            print(f'Adding …{conf.adding.number[-4:]}: stopped by you',flush=True)
            await self.hangup(conf.adding)

    async def start_conference(self,call):
        cfg=self.registration.config
        factory=self.factory_uri()
        conf=Call(secrets.token_urlsafe(18),secrets.token_hex(16)+'@fiber-gateway','outgoing','Conference',factory)
        conf.conference=True;conf.previous=call;conf.invite_started=True
        conf.local_from=f'<{cfg["publicIdentity"]}>;tag={conf.local_tag}';conf.remote_to='<'+factory+'>'
        conf.media=await self.new_media()
        conf.media.on_error=lambda error:conf.spawn(self.media_failed(conf,error))
        conf.participants=[{'number':call.number,'status':'joining','error':None,'replaces':True}]
        self.current=conf;self.remember(conf)
        await self.invite(conf)
        if conf.state!='active':
            self.current=call
            print(f'Conference factory refused: {conf.error}',flush=True)
            for line in conf.trace:print('  '+line,flush=True)
            raise ValueError('Could not start a conference: '+(conf.error or 'no answer'))
        await self.move_audio(call,conf)
        print(f'Conference focus: {masked(conf.target)}',flush=True)
        targets=self.replaces_targets(call)
        plan=[(t,False) for t in targets]+([(targets[0],True)] if targets else [])
        error=None
        for target,hold_first in plan:
            if hold_first and not await self.hold(call):break
            conf.participants[0].update(status='joining',error=None)
            try:
                await self.refer(conf,conf.participants[0],target)
                return conf
            except Exception as failure:
                error=failure
        await self.restore_call(conf,f'Could not move the call into the conference ({error})')
        raise ValueError('Could not move this call into a conference')

    async def refer(self,conf,participant,target):
        extra=[f'Refer-To: <{target}>',f'Referred-By: <{self.registration.config["publicIdentity"]}>']
        code,headers,_=await self.authenticated(conf,'REFER',extra=extra)
        if not 200<=code<300:
            # Diagnostic log with numbers masked: what we asked for and how the server answered.
            details=' | '.join(f'{name}: {value}' for name in ('reason','warning','server','retry-after','allow','supported','require') for value in headers.get(name,[]))
            print(f'REFER refused: SIP {code}'+(f' | {details}' if details else ''),flush=True)
            print(f'  Refer-To: {masked(target)}',flush=True)
            participant.update(status='failed',error=SIP_TEXT.get(code) or f'Not added (SIP {code})')
            raise ValueError(f'REFER rejected with SIP {code}')
        seq=first(headers,'cseq').split()[0]
        if seq.isdigit():conf.refers[int(seq)]=participant

    def refer_notify(self,conf,headers,body):
        match=re.match(r'\s*refer\b(?:.*?;\s*id\s*=\s*(\d+))?',first(headers,'event'),re.I)
        frag=re.match(r'\s*SIP/2\.0\s+(\d{3})',body.decode(errors='replace'))
        if not match or not frag:return
        participant=conf.refers.get(int(match[1])) if match[1] else next((p for p in reversed(conf.participants) if p['status'] not in ('joined','failed')),None)
        if not participant:return
        code=int(frag[1])
        if code<180:participant['status']='calling' if not participant.get('replaces') else 'joining'
        elif code<200:participant['status']='ringing'
        elif code<300:participant.update(status='joined',error=None)
        else:
            participant.update(status='failed',error=SIP_TEXT.get(code) or f'Could not join (SIP {code})')
            if participant.get('replaces'):
                conf.spawn(self.restore_call(conf,'The other person could not be moved into the conference'))

    async def restore_call(self,conf,reason):
        """Undo a conference that could not take over the original call."""
        call=conf.previous
        print(f'Conference cancelled: {reason}',flush=True)
        restore=call is not None and call.state=='active'
        if restore:await self.move_audio(conf,call)  # back to the original call's audio first
        if conf.state in LIVE:
            conf.state='ending';await self.bye(conf);await self.finish(conf,'ended')
        if restore:
            self.current=call
            call.error=reason

    async def close(self):
        if self.current and self.current.state in LIVE:
            await self.hangup()
            if not self.current.ended:await self.finish(self.current,'ended')
        for task in list(self.background):task.cancel()
        await asyncio.gather(*list(self.background),return_exceptions=True)
