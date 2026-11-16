# Gateway (developer reference)

The gateway is a small Python server. It registers with the JioFiber router as an independent voice client over SIP/TLS, and it converts between browser WebRTC audio (Opus) and the AMR / AMR-WB audio that the landline network uses. It serves the dialer page on `http://127.0.0.1:8765/`. The Mac app bundles this same gateway.

For installing and using Fiber Voice, see the [main README](../README.md).

## Run from source

```sh
brew install opencore-amr vo-amrwbenc        # macOS; Linux: libopencore-amrnb0 libopencore-amrwb0 libvo-amrwbenc0
./run.sh                                      # creates .venv and installs requirements on first run
```

Open <http://127.0.0.1:8765/>. Press Ctrl+C to stop; the gateway deregisters only its own SIP contact on the way out.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8765` | Local HTTP port (always bound to 127.0.0.1) |
| `GATEWAY_RUNTIME` | `./.runtime` | Private settings folder (0700, files 0600) |
| `GATEWAY_LIB_DIR` | — | Folder with bundled codec dylibs (used by the Mac app) |
| `GATEWAY_PARENT_PID` | — | Exit when this parent process goes away (used by the Mac app) |

## Modules

| File | What it does |
| --- | --- |
| `server.py` | aiohttp app, local-only security middleware, JSON API, WebRTC peers |
| `onboarding.py` | First-run setup: router certificate pinning, OTP authorization, provisioning |
| `registration.py` | SIP Digest REGISTER over IPv6/TLS with renewal, reconnection and pin checks |
| `calls.py` | One-call SIP dialog controller: INVITE/PRACK/ACK/CANCEL/BYE/UPDATE, early dialogs, failure reasons |
| `media.py` | AMR/AMR-WB codecs (ctypes), RFC 4867 framing, RTP/RTCP, jitter buffer, RFC 4733 DTMF, SDP answers mirroring the offer |
| `calls.py` (conference) | Network conferences: INVITE to the conference factory, REFER with `Replaces` to move the current call in, plain REFER to add people, progress from REFER NOTIFYs |
| `video.py` | H.264 video bridge: RFC 6184 packetization mode 1 over RTP/AVPF, 640×480/320×240 at 15 fps, RTCP PLI keyframe requests |
| `history.py` | Private call log with missed/blocked/silenced/unreachable results |
| `contacts.py` | Names saved in the app and favourites |
| `screening.py` | Block list and Do Not Disturb (manual, timed, nightly schedule, repeated callers) |
| `enroll.py` | Terminal version of the setup flow |
| `desktop.py` | Entry point for the Windows app: free loopback port, readiness file, exits when the app closes |
| `static/` | The dialer UI: desktop layout, setup wizard, recents, contacts, settings |

## Private data

Everything lives in `GATEWAY_RUNTIME`: `router-trust.json`, `client-identity.json`, `provisioning.json` (SIP credentials), `call-history.json`, `contacts.json` and `screening.json`. The folder is Git-ignored, never served over HTTP, and the API never returns passwords, Digest headers, cookies or OTPs. **Log out** deletes the trust pin, provisioning, call log, saved contacts and screening settings.

## API (local only)

All endpoints reject foreign `Host`/`Origin` and cross-site requests.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/setup`, `GET /api/setup/router` | Setup state; inspect the router certificate |
| `POST /api/setup/trust`, `/otp`, `/verify`, `/reset` | Trust the router, request and verify the OTP, log out |
| `GET /api/registration/status`, `POST /api/registration/retry` | SIP registration |
| `POST /api/calls/{id}/participants` | Add someone (turns the call into a conference the first time) |
| `POST /api/calls/{id}/hold` | Hold (`{"hold": true}`) or resume the connected call |
| `POST /api/calls/{id}/answer-waiting`, `POST /api/calls/swap` | Hold the current call and answer the waiting one; switch between the two calls |
| `POST /api/calls`, `POST /api/calls/{id}/answer`, `POST /api/calls/{id}/dtmf`, `DELETE /api/calls/{id}`, `GET /api/calls/current` | Calls (a WebRTC offer goes in the body) |
| `GET/DELETE /api/history`, `POST /api/history/seen`, `DELETE /api/history/{id}` | Call log |
| `GET/PUT /api/contacts`, `PUT /api/favorites` | Saved names and favourites |
| `GET /api/screening`, `POST /api/blocked`, `DELETE /api/blocked/{key}`, `PUT /api/dnd` | Block list and Do Not Disturb |
| `GET /api/status` | Router and account diagnostics |
| `POST /api/audio/offer`, `DELETE /api/audio/{id}` | Local microphone/speaker loopback test |

## Protocol notes

- **Setup.** The router's provisioning service (`jiofiber.local.html:8443`) answers HTTP **407** for an unknown client, which means "not whitelisted". Repeating the request with `op_type=add` sends an OTP to the registered mobile. Submitting the OTP returns XML provisioning with the SIP identity, credentials and proxy. Some firmware puts blank lines before `Set-Cookie`; the parser handles this. Encrypted provisioning from other firmware is not supported.
- **Registration.** The proxy is the router itself, over IPv6/TLS on port 5068. The router-issued `+sip.instance` value must be sent exactly as given, inside `<>`; changing it gets `403 Device not whitelisted`. Credentials are only sent to a router whose certificate matches the saved pin.
- **Dialing.** As in Jio's own client, ten-digit numbers get a leading `0`. `+91…` numbers are sent unchanged.
- **Early dialogs.** The network sends `UPDATE`/`INFO`/`OPTIONS` while the callee's phone is ringing. These must be accepted (200 OK, with an SDP answer for UPDATE). Rejecting them with 481 makes the network drop the call with 480 after a few seconds.
- **Failed calls.** Q.850 causes from `Reason` headers and SIP codes are mapped to plain messages. A number-masked SIP trace (last 4 digits plus length) goes to stdout, which is the Mac app's `gateway.log`.
- **Screening.** Blocked callers get `603 Decline`. Do Not Disturb answers `480 Temporarily Unavailable`, so this client stays silent while the line's other devices keep ringing.
- **Hold.** A hold re-INVITE re-offers the call's negotiated codec with `a=sendonly`, and resume uses `a=sendrecv`, as Jio's client builds them. Both directions go silent until resumed.
  - After a hold or resume, the network often sends its own re-INVITE **without SDP**. The 200 OK must then carry our offer (`sendonly` while held, otherwise `sendrecv`), and the ACK brings the answer. Answering it without SDP leaves the far phone on hold.
  - If the network is updating the call at the same moment, our re-INVITE gets 487/491/500. It is acknowledged and retried after 1–2.5 s, up to 3 times. A resume that still fails shows the call as on hold, so Resume can be pressed again.
  - Answers to the network's offers keep our hold (`sendonly` or `inactive`) while held.
- **Conference.** The factory address comes from provisioning (`factory`), or `sip:mmtel@conf-factory.<domain>` if missing. The focus is the JioFiber router itself, which only merges calls that already exist. A REFER asking it to dial a new number gets 500. Adding someone therefore works like a phone:
  1. The first time, INVITE the factory and move the current call in with `Refer-To: <sip:+91…@<domain>;user=phone;method=INVITE?Replaces=call-id;to-tag;from-tag>`. This is the exact form Jio's client builds in `VvmSession::initiateInDialogRefer`, and it's verified live.
  2. Hold the conference leg (re-INVITE `a=sendonly`), call the new person, and on answer REFER their call in the same way.
  3. Resume the conference leg.

  All three steps are verified on a live call: two people merged and joined.

  Browser audio follows whichever leg is active (`replaceTrack`). If the first move fails, audio returns to the original call.
- **Video.** The offer is H.264 Constrained Baseline (`42c01f` at 640×480 and `42c00c` at 320×240), `packetization-mode=1`, `RTP/AVPF` with `nack pli` and `imageattr`, matching Jio's client. Video calls add the `;video` feature tag to Contact and an `Accept-Contact` header. Answers always mirror the offer's media lines: unused video is declined with port 0. Incoming video can be answered voice-only. This hasn't been verified against a live video call yet.
- **Media.** The browser leg uses WebRTC ICE/DTLS-SRTP/Opus. The network leg uses AMR-WB or AMR, octet-aligned or bandwidth-efficient, over RTP/AVP on IPv6 ports 52000–52200. G.711 (PCMA/PCMU) is used only when a caller offers no AMR: Jio's network handles hold/resume re-INVITEs on AMR calls but not reliably on G.711 calls.
- **Incoming calls.** No RTP is sent until the call is answered. The 200 OK confirms the caller's session timer (`Session-Expires …;refresher=uac`, `Require: timer`) when it is offered.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js static/setup.js
```

The suite covers:
- Full WebRTC → AMR RTP → WebRTC round trips with non-silent audio in both directions.
- Every AMR/AMR-WB mode and payload format.
- Digest, PRACK, CANCEL races, late answers, incoming answer/cancel, and early-dialog UPDATE/INFO handling, including an AMR-WB → AMR switch when the call is answered.
- Conference calls (moving the call in, adding people, busy and failed adds) and H.264 video in both directions.
- Call waiting (Hold & Answer, Swap, End & Answer, decline, the caller hanging up, busy cases) and hold/resume: the `a=sendonly` hold offer, re-INVITEs without SDP, retry after a collision, and answers that keep the hold.
- G.711 codec choice and a PCMA RTP round trip.
- Failure-reason mapping.
- Registration renewal and certificate-change rejection.
- The OTP setup flow.
- Call log, contacts, favourites, blocking, Do Not Disturb schedules, and local-only API protection.

## Scope

One call at a time (a conference counts as one call), over the connection's IPv6 network. Video calls are a beta. Call transfer and text messages are not implemented. Incoming calls need an SDP audio offer.

**Call waiting.** A second incoming call during a connected voice call rings as a waiting call (`waiting` in `GET /api/calls/current`). Anything after that gets 486 Busy, as do calls arriving during setup, video calls or a conference add. The waiting call can be:
- answered with the current call on hold (*Hold & Answer*), after which *Swap* switches between them and the browser audio follows the connected call;
- answered after ending the current call (*End & Answer*);
- declined.

If the connected call ends, the held call stays connected and on hold until resumed.

## References

[RFC 3261 (SIP)](https://www.rfc-editor.org/rfc/rfc3261) · [RFC 3262 (PRACK)](https://www.rfc-editor.org/rfc/rfc3262) · [RFC 3311 (UPDATE)](https://www.rfc-editor.org/rfc/rfc3311) · [RFC 4867 (AMR RTP)](https://www.rfc-editor.org/rfc/rfc4867) · [RFC 4733 (DTMF)](https://www.rfc-editor.org/rfc/rfc4733) · [OpenCORE AMR](https://github.com/BelledonneCommunications/opencore-amr) · [VisualOn AMR-WB encoder](https://github.com/mstorsjo/vo-amrwbenc)
