"""Check container codecs, IPv6 router reachability and (if set up) the saved pin; never call or REGISTER."""
import hashlib
import hmac
import ipaddress
import json
import socket
import ssl

from media import codec_capabilities
from registration import CONFIG, RUNTIME


def main():
    failed = False
    codecs = codec_capabilities()
    if codecs['ready']:
        print('OK: AMR-NB and AMR-WB native codecs')
    else:
        print('FAIL: ' + codecs['error'])
        failed = True
    try:
        pin = None
        host, port = 'jiofiber.local.html', 5068
        saved = [name for name in ('provisioning.json', 'client-identity.json', 'router-trust.json')
                 if (RUNTIME / name).is_file()]
        if not saved:
            print('OK: no saved setup yet; finish setup in the browser after starting the gateway')
        elif len(saved) < 3 and 'provisioning.json' in saved:
            raise ValueError('Incomplete saved setup; copy all three private files or none')
        if CONFIG.is_file():
            config = json.loads(CONFIG.read_text())
            trust = json.loads((RUNTIME / 'router-trust.json').read_text())
            pin = config['certificateSha256']
            if not hmac.compare_digest(pin, trust['certificateSha256']):
                raise ValueError('Provisioning and router trust pins differ')
            host, port = config['proxyHost'], int(config['proxyPort'])
            if host != 'jiofiber.local.html':
                raise ValueError('Unexpected provisioned router hostname')
            print('OK: saved provisioning, identity and trust files are readable')
        addresses = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)
        errors = []
        for _, kind, protocol, _, address in addresses:
            try:
                with socket.socket(socket.AF_INET6, kind, protocol) as raw:
                    raw.settimeout(5)
                    raw.connect(address)
                    # Do not send credentials or SIP messages during this check.
                    context = ssl._create_unverified_context()
                    with context.wrap_socket(raw, server_hostname=host) as tls:
                        actual = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
                        if pin and not hmac.compare_digest(actual, pin):
                            raise ValueError('Router certificate differs from saved pin')
                        local = tls.getsockname()[0]
                        ip = ipaddress.IPv6Address(local.split('%', 1)[0])
                        if ip.is_loopback or ip.is_unspecified or ip.is_link_local:
                            raise ValueError('SIP did not select a routable LAN IPv6 address')
                        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as udp:
                            udp.bind((local, 0))
                print('OK: IPv6 router TLS connection' + (', saved certificate pin' if pin else '') + ' and local UDP binding')
                print('Network prerequisites passed. This does not verify registration or call audio.')
                return int(failed)
            except (OSError, ValueError) as error:
                errors.append(str(error))
        raise ValueError('; '.join(errors) or 'No IPv6 router address')
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('FAIL: ' + str(error))
        print('Check the private runtime mount, Fiber DNS/IPv6 and WSL mirrored networking.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
