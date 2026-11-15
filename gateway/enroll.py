"""Authorize this local gateway through the router's own OTP flow (terminal).

Run interactively only when the gateway has not yet been provisioned. Existing
provisioning is reused by server.py; it does not need another OTP on every start.
The Mac app and web page offer the same flow on their setup screen.
"""
import argparse
import getpass

from onboarding import Onboarding, SetupError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace', action='store_true', help='Explicitly reauthorize this gateway')
    parser.add_argument('--trust', action='store_true',
                        help='Trust the certificate the router presents now (first setup only)')
    args = parser.parse_args()
    setup = Onboarding()
    if setup.config_path.exists() and not args.replace:
        raise SystemExit('Gateway already provisioned. Start run.sh; no new OTP is needed.')
    try:
        if not setup.fingerprint():
            if not args.trust:
                details = setup.inspect_router()
                raise SystemExit('No router trust pin saved. Router certificate SHA-256:\n  '
                                 + details['certificateSha256'] + '\nRe-run with --trust on your own '
                                 'JioFiber network to trust it.')
            setup.trust(setup.inspect_router()['certificateSha256'])
        result = setup.start()
        if result['state'] == 'otp_sent':
            print('OTP sent to', result.get('otpTarget') or 'the registered mobile')
            setup.verify(getpass.getpass('Enter the exact OTP received: ').strip())
    except SetupError as error:
        raise SystemExit(str(error))
    print('Gateway provisioned. Start run.sh or use Retry registration in the local page.')


if __name__ == '__main__':
    main()
